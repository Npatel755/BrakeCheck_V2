import BrakeCheckCore
import CoreGraphics
import CoreImage
import CoreML
import CoreVideo
import CryptoKit
import Foundation

/// The marking fallback runs on VehicleTracker's queue. UFLD and the Intel road model
/// each retain at most one camera frame on independent queues. Segmentation
/// decoding cannot hold the lane worker busy after a UFLD observation is ready.
/// The shared prediction gate still gives vehicle requests priority.
final class LaneTracker {
    var onStatus: ((String) -> Void)?
    private let modelQueue = DispatchQueue(label: "BrakeCheck.lane-model", qos: .utility,
                                          autoreleaseFrequency: .workItem)
    private let roadQueue = DispatchQueue(label: "BrakeCheck.road-model", qos: .utility,
                                         autoreleaseFrequency: .workItem)
    private let stateLock = NSLock()
    private let inference: VisionInferenceCoordinator
    private var minimumInterval: TimeInterval = 0.20
    private var roadInterval: TimeInterval? = 0.30
    private var lastRoadSubmission: TimeInterval = -.infinity
    private var laneLatencyHistory = AuxiliaryLatencyHistory()
    private var roadLatencyHistory = AuxiliaryLatencyHistory()
    private var laneAllowed = true
    private var arProcessingEnabled = false
    func setARProcessingEnabled(_ enabled: Bool) {
        stateLock.lock(); arProcessingEnabled = enabled; stateLock.unlock()
        arScene = nil; arSceneTracker.reset(); lastProjectedRoad = nil
    }
    func configureBudget(minimum: TimeInterval, heat: VisionHeat, vehicleLatency: TimeInterval, stationary: Bool) {
        stateLock.lock()
        let policy = ARInferencePolicy(minimumLaneInterval: minimum, heat: heat, vehicleLatency: vehicleLatency,
                                       laneLatency: laneLatencyHistory.seconds(at: ProcessInfo.processInfo.systemUptime),
                                       roadLatency: roadLatencyHistory.seconds(at: ProcessInfo.processInfo.systemUptime), stationary: stationary)
        minimumInterval = max(policy.laneInterval, arProcessingEnabled ? 0.20 : 1.0 / 3)
        roadInterval = policy.roadInterval.map { max($0, arProcessingEnabled ? 0.30 : 0.50) }
        laneAllowed = policy.laneAllowed
        stateLock.unlock()
    }

    init(inference: VisionInferenceCoordinator) { self.inference = inference }
    func setMinimumInterval(_ interval: TimeInterval) {
        guard interval.isFinite, (0.20...0.50).contains(interval) else { return }
        stateLock.lock(); minimumInterval = interval; stateLock.unlock()
    }
    private var loadingStarted = false
    private var modelReady = false
    private var modelBusy = false
    private var roadBusy = false
    private var generation = 0
    private var lastSubmission: TimeInterval = -.infinity
    private var neuralMeasurements = LaneMeasurementBuffer()
    private var leadLaneMeasurements = LeadLaneEvidenceBuffer()
    private var leadLaneState = "waiting for lane pair"
    func leadLaneDiagnostic(at time: TimeInterval) -> String {
        stateLock.lock(); defer { stateLock.unlock() }
        if leadLaneMeasurements.latest(at: time) != nil { return "Lead lane · confirmed temporal pair" }
        if leadLaneMeasurements.latestBoundaries(at: time) != nil { return "Lead lane · observed boundary constraints" }
        if leadLaneState == "confirmed observed lane pair" { return "Lead lane · pair expired" }
        return "Lead lane · \(leadLaneState)"
    }
    // Display-only evidence never enters corridor fitting, collision or AR.
    private var rawPrediction: LanePredictionObservation?
    private var diagnostic = "Loading lanes"
    private var roadLoadingStarted = false
    private(set) var lanePredictions: LanePredictionObservation?
    var laneDiagnostic: String {
        stateLock.lock(); defer { stateLock.unlock() }
        return modelReady && !laneAllowed ? "Lanes paused for performance" : diagnostic
    }
    private var roadMeasurements: [(time: TimeInterval, region: RoadRegion?, mask: RoadVisibilityMask?,
                                    frame: LaneLumaFrame, lane: LanePath?)] = []
    private var vehicleBoxes: [CGRect] = []
    private var boxesTime: TimeInterval = -.infinity

    // Lane inference state is owned by modelQueue. The road model reference is
    // published under stateLock; roadInputPool is owned by roadQueue. CIContext
    // supports concurrent rendering on the two workers.
    private var model: MLModel?
    private var roadModel: MLModel?
    private var neuralEstimator = UFLDv2EgoCorridorEstimator()
    private var leadLaneEstimator = LeadLaneEvidenceEstimator()
    private var displaySmoother = LanePredictionSmoother()
    private var kalmanSmoother = LaneKalmanSmoother()
    private var displayTrackingMode = LaneTrackingMode.kalman
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var inputPool: CVPixelBufferPool?
    private var lastStatusTime: TimeInterval = -.infinity
    private var consecutiveErrors = 0
    private var inputWidth = UFLDv2InputTransform.inputWidth
    private var inputHeight = UFLDv2InputTransform.inputHeight
    private var roadInputPool: CVPixelBufferPool?

    // These properties are used exclusively on VehicleTracker's queue.
    private var estimator = LaneEstimator()
    private var lastScan: TimeInterval = -.infinity
    private var latest: LanePath?
    private(set) var roadRegion: RoadRegion?
    private(set) var arScene: CameraARScene?
    private var arSceneTracker = CameraARSceneTracker()
    private var lastVisualUpdate: TimeInterval = -.infinity
    private var lastTrackedLane: LanePath?
    private var lastTrackedSourceTime: TimeInterval?
    // Vehicle-queue publication remembers the real source, even when optical
    // projection has a newer timestamp than the underlying model observation.
    private var publishedNeuralSourceTime: TimeInterval?
    private var lastProjectedRoad: RoadRegion?
    private var projectedRoadSource: TimeInterval = -.infinity

    func prepare() {
        stateLock.lock()
        if modelReady {
            stateLock.unlock()
            // Lane success does not make a failed independent road load
            // permanent. That worker guards both readiness and in-flight work.
            prepareRoadModel()
            return
        }
        guard !loadingStarted else { stateLock.unlock(); return }
        loadingStarted = true
        stateLock.unlock()
        modelQueue.async {
            self.onStatus?("UFLDv2 + Intel road model loading…")
            do {
                let source = try self.locateModel(named: "UFLDv2Lane")
                let compiled = try self.compiledModel(from: source)
                let options = MLModelConfiguration()
                options.computeUnits = .all
                let model: MLModel
                do { model = try MLModel(contentsOf: compiled, configuration: options) }
                catch {
                    // A interrupted/old compilation must not permanently force
                    // the fallback. Rebuild only this model's hashed cache.
                    try FileManager.default.removeItem(at: compiled)
                    model = try MLModel(contentsOf: self.compiledModel(from: source), configuration: options)
                }
                let outputShapes = [
                    "loc_row": [1, 200, 72, 4], "loc_col": [1, 100, 81, 4],
                    "exist_row": [1, 2, 72, 4], "exist_col": [1, 2, 81, 4]
                ]
                guard let image = model.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
                      image.pixelsWide == UFLDv2InputTransform.inputWidth,
                      image.pixelsHigh == UFLDv2InputTransform.inputHeight,
                      outputShapes.allSatisfy({ name, shape in
                          model.modelDescription.outputDescriptionsByName[name]?.multiArrayConstraint?.shape.map({ $0.intValue }) == shape
                      }) else {
                    throw LaneModelError.contract
                }
                self.inputWidth = image.pixelsWide; self.inputHeight = image.pixelsHigh
                self.model = model
                self.stateLock.lock()
                self.modelReady = true; self.diagnostic = "Lane model ready"
                self.stateLock.unlock()
                self.onStatus?("UFLDv2 lanes ready · road model loading")
                self.prepareRoadModel()
            } catch {
                self.stateLock.lock()
                self.loadingStarted = false; self.modelReady = false
                self.rawPrediction = nil; self.diagnostic = "Lane model error"
                self.stateLock.unlock()
                self.onStatus?("UFLDv2/Intel road model unavailable: \(error.localizedDescription) · paint fallback")
            }
        }
    }

    /// Lane readiness does not wait for independent road-model compilation.
    /// Road inference and loading stay on their own queue; publication of the
    /// shared immutable model reference is protected by stateLock.
    private func prepareRoadModel() {
        stateLock.lock()
        guard roadModel == nil, !roadLoadingStarted else { stateLock.unlock(); return }
        roadLoadingStarted = true
        stateLock.unlock()
        roadQueue.async {
            defer {
                self.stateLock.lock(); self.roadLoadingStarted = false; self.stateLock.unlock()
            }
            do {
                let source = try self.locateModel(named: "IntelRoad")
                let compiled = try self.compiledModel(from: source)
                let options = MLModelConfiguration(); options.computeUnits = .all
                let road: MLModel
                do { road = try MLModel(contentsOf: compiled, configuration: options) }
                catch {
                    try FileManager.default.removeItem(at: compiled)
                    road = try MLModel(contentsOf: self.compiledModel(from: source), configuration: options)
                }
                guard let image = road.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
                      image.pixelsWide == 640, image.pixelsHigh == 384,
                      road.modelDescription.outputDescriptionsByName["road_scores"]?.multiArrayConstraint?.shape.map({ $0.intValue }) == [1, 2, 384, 640] else { throw LaneModelError.contract }
                self.stateLock.lock(); self.roadModel = road; self.stateLock.unlock()
                self.onStatus?("UFLDv2 lanes + Intel road ready")
            } catch {
                self.stateLock.lock(); self.roadModel = nil; self.stateLock.unlock()
                self.onStatus?("UFLDv2 lanes ready · road segmentation unavailable: \(error.localizedDescription)")
            }
        }
    }

    func reset() {
        estimator.reset(); latest = nil; roadRegion = nil; arScene = nil; lastScan = -.infinity
        arSceneTracker.reset()
        lastVisualUpdate = -.infinity; lastTrackedLane = nil; lastProjectedRoad = nil
        lastTrackedSourceTime = nil
        publishedNeuralSourceTime = nil
        stateLock.lock()
        generation += 1
        neuralMeasurements.reset()
        leadLaneMeasurements.reset()
        leadLaneState = "waiting for lane pair"
        rawPrediction = nil; lanePredictions = nil
        if modelReady { diagnostic = "Lane model ready" }
        roadMeasurements = []
        vehicleBoxes = []; boxesTime = -.infinity
        lastSubmission = -.infinity
        lastRoadSubmission = -.infinity
        // Keep each busy flag true until its old prediction releases its frame.
        // The generation prevents either worker publishing into a new trip.
        stateLock.unlock()
        modelQueue.async { self.neuralEstimator.reset(); self.leadLaneEstimator.reset(); self.displaySmoother.reset(); self.kalmanSmoother.reset() }
    }

    /// Cheap vehicle-queue check before using cached geometry for lead selection.
    /// A completed negative/replacement observation can arrive during detection,
    /// before the next expensive lane projection updates the cached LanePath.
    func validatedGuidance(_ lane: LanePath?, at time: TimeInterval) -> LanePath? {
        guard let lane, lane.isUsable(at: time) else { return nil }
        if lane.source == .markings { return lane }
        guard let origin = publishedNeuralSourceTime else { return nil }
        stateLock.lock()
        let current = neuralMeasurements.latest(at: time)?.lane.capturedAt
        stateLock.unlock()
        return current == origin ? lane : nil
    }

    /// Lead-specific evidence is independent of guide visibility/style/mode.
    /// Strict stock guidance remains preferred and collision/AR keep that path.
    func leadGuidance(_ stock: LanePath?, at time: TimeInterval) -> LanePath? {
        if let stock = validatedGuidance(stock, at: time) { return stock }
        stateLock.lock(); defer { stateLock.unlock() }
        return leadLaneMeasurements.latest(at: time)
    }
    func leadBoundaryGuidance(at time: TimeInterval) -> LanePredictionObservation? {
        stateLock.lock(); defer { stateLock.unlock() }
        return leadLaneMeasurements.latestBoundaries(at: time)
    }

    func update(_ buffer: CVPixelBuffer, excluding boxes: [CGRect], at time: TimeInterval) -> LanePath? {
        let now = max(time, ProcessInfo.processInfo.systemUptime)
        stateLock.lock()
        vehicleBoxes = boxes; boxesTime = time
        let measurement = neuralMeasurements.latest(at: time)
        let raw = rawPrediction
        let roadObservation = roadMeasurements.last { $0.time <= time }
        let road = roadObservation?.region
        let arEnabled = arProcessingEnabled
        stateLock.unlock()
        // A newer negative lane observation must also end a cached projection.
        if measurement == nil {
            lastTrackedLane = nil; lastProjectedRoad = nil; projectedRoadSource = -.infinity
            lastTrackedSourceTime = nil
        }
        lanePredictions = LanePredictionDisplay.visible(raw, at: now, excluding: boxes)
        roadRegion = road.flatMap { $0.isUsable(at: now) ? $0 : nil }
        let visualDue = time - lastVisualUpdate >= 0.19
        if visualDue { lastVisualUpdate = time }
        let needsFrame = measurement != nil || (arEnabled && roadObservation?.lane != nil)
        let frame = visualDue && needsFrame ? lumaFrame(buffer, at: time) : nil
        var tracked: LanePath?
        if let measurement, let frame {
            tracked = LaneVisualTracker.project(measurement.lane, from: measurement.frame, to: frame, excluding: boxes)
            if let tracked {
                lastTrackedLane = tracked; lastTrackedSourceTime = measurement.lane.capturedAt
            }
        } else if !visualDue, lastTrackedSourceTime == measurement?.lane.capturedAt {
            tracked = lastTrackedLane.flatMap { $0.isUsable(at: now) && time - $0.capturedAt <= 0.25 ? $0 : nil }
        }
        var arRoad = roadRegion
        if arEnabled, let observation = roadObservation, let road = observation.region,
           time - observation.time > 0.02, time - observation.time <= 0.8,
           let originalLane = observation.lane,
           measurement?.lane.capturedAt == originalLane.capturedAt, let frame {
            let sameSource = observation.frame.capturedAt == measurement?.frame.capturedAt
            let trackedLane = sameSource ? tracked
                : LaneVisualTracker.project(originalLane, from: observation.frame, to: frame, excluding: boxes)
            // AR-only visual prediction. Never feed a warped road into safety
            // eligibility, and always warp back to the original inference.
            arRoad = trackedLane.flatMap { RoadVisualProjection.project(road, from: originalLane, to: $0, at: time) }
            lastProjectedRoad = arRoad; projectedRoadSource = observation.time
        } else if arEnabled, !visualDue, projectedRoadSource == roadObservation?.time,
                  measurement?.lane.capturedAt == roadObservation?.lane?.capturedAt,
                  let cached = lastProjectedRoad, cached.isUsable(at: now) {
            arRoad = cached
        } else if let observation = roadObservation, time - observation.time > 0.35 {
            arRoad = nil
        }
        func publish(_ lane: LanePath?, arLane: LanePath? = nil) -> LanePath? {
            publishedNeuralSourceTime = lane == nil || lane?.source == .markings
                ? nil : measurement?.lane.capturedAt
            guard arEnabled else { return lane }
            // AR uses the current visually tracked neural corridor directly;
            // collision lane fusion retains its original timestamps/behavior.
            let scene = CameraARScene(lane: arLane ?? lane, road: arRoad,
                laneObservedAt: lane?.source == .markings ? (lane?.capturedAt ?? -.infinity)
                    : (measurement?.lane.capturedAt ?? lane?.capturedAt ?? -.infinity),
                roadObservedAt: roadObservation?.time ?? -.infinity, capturedAt: time,
                diagnosticRoadMask: roadObservation.flatMap { time - $0.time <= 0.5 ? $0.mask : nil })
            arScene = arSceneTracker.update(scene, at: time)
            return lane
        }
        // Keep both observers running. A complete paint section can extend a
        // matching short model section; the fallback no longer stays cold
        // while a partial neural lane monopolizes the output.
        let markings = scanMarkings(buffer, excluding: boxes, at: time)
        if let measurement, time >= measurement.lane.capturedAt,
           time - measurement.lane.capturedAt <= 1.0 {
            if let tracked, tracked.isUsable(at: now) {
                return publish(LaneFusion.combine(neural: tracked, markings: markings, at: now), arLane: tracked)
            }
            // A failed visual match does not renew the model's capture time.
            if measurement.lane.isUsable(at: now) {
                return publish(LaneFusion.combine(neural: measurement.lane, markings: markings, at: now))
            }
        }
        return publish(LaneFusion.combine(neural: nil, markings: markings, at: now))
    }

    /// Offered after vehicle inference/rear scheduling, or on a cadence-skipped
    /// capture. The independent model queue retains at most one frame.
    func submit(_ buffer: CVPixelBuffer, at time: TimeInterval) {
        stateLock.lock()
        guard modelReady, laneAllowed, !modelBusy, time.isFinite, time - lastSubmission >= minimumInterval - 1e-6 else {
            stateLock.unlock(); return
        }
        modelBusy = true; lastSubmission = time
        let submittedGeneration = generation
        let arRequested = arProcessingEnabled
        let boxes = time >= boxesTime && time - boxesTime <= 0.25 ? vehicleBoxes : []
        stateLock.unlock()
        modelQueue.async {
            defer {
                self.stateLock.lock(); self.modelBusy = false; self.stateLock.unlock()
            }
            self.stateLock.lock()
            let current = self.generation == submittedGeneration
            self.stateLock.unlock()
            guard current, let model = self.model else { return }
            // A delayed frame is discarded before doing more work.
            guard ProcessInfo.processInfo.systemUptime - time <= 0.30 else {
                self.stateLock.lock()
                if self.generation == submittedGeneration { self.diagnostic = "Lane prediction delayed" }
                self.stateLock.unlock()
                return
            }
            let started = ProcessInfo.processInfo.systemUptime
            do {
                guard let prediction = try self.inference.auxiliary({
                    let trace = PerformanceTrace.begin(.laneInference)
                    defer { PerformanceTrace.end(trace) }
                    let input = try self.makeInput(buffer, width: self.inputWidth, height: self.inputHeight, pool: &self.inputPool, laneCrop: true)
                    guard let frame = self.lumaFrame(buffer, at: time) else { throw LaneModelError.frame }
                    let provider = try MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: input)])
                    return (try model.prediction(from: provider), frame)
                }) else {
                    // No waiting frame: retry on a newer capture soon, rather
                    // than spending the entire lane interval on a dropped job.
                    self.stateLock.lock()
                    if self.generation == submittedGeneration, self.lastSubmission == time {
                        self.lastSubmission = time - self.minimumInterval + 0.05
                        self.diagnostic = "Lanes paused for performance"
                    }
                    self.stateLock.unlock()
                    return
                }
                let (result, frame) = prediction
                // Decode and publish lanes before waiting for segmentation.
                // Road-model failure/cadence must not suppress UFLDv2 observations.
                let decoded = try self.decode(result)
                let lane = self.neuralEstimator.update(decoded.ego, excluding: boxes, at: time)
                let observation = LanePredictionDisplay.observation(candidates: decoded.display, at: time)
                let geometry = LanePredictionGeometry.filtered(observation)
                // Use real pre-temporal observations, with independent pair
                // confirmation; visual Kalman/filled output never enters here.
                let leadPair = self.leadLaneEstimator.update(
                    LanePredictionDisplay.visible(geometry, at: time, excluding: boxes), at: time)
                let mode = LaneTrackingMode(rawValue: UserDefaults.standard.string(forKey: "display.laneTrackingMode") ?? "kalman") ?? .kalman
                if mode != self.displayTrackingMode {
                    self.displaySmoother.reset(); self.kalmanSmoother.reset()
                    self.displayTrackingMode = mode
                }
                let displayed: LanePredictionObservation?
                switch mode {
                case .raw: displayed = geometry
                case .averaging: displayed = self.displaySmoother.update(geometry, at: ProcessInfo.processInfo.systemUptime)
                case .kalman: displayed = self.kalmanSmoother.update(geometry, at: ProcessInfo.processInfo.systemUptime)
                }
                let finishedAt = ProcessInfo.processInfo.systemUptime
                self.stateLock.lock()
                // Include decoding/presentation work in the auxiliary budget.
                self.laneLatencyHistory.observe(finishedAt - started)
                if self.generation == submittedGeneration {
                    self.leadLaneMeasurements.record(leadPair,
                        boundaries: LanePredictionDisplay.visible(geometry, at: time, excluding: boxes), at: time)
                    self.leadLaneState = self.leadLaneEstimator.status
                    self.rawPrediction = LanePredictionDisplay.visible(displayed, at: finishedAt)
                    self.diagnostic = finishedAt - time > 0.5 ? "Lane prediction delayed"
                        : observation != nil && geometry == nil ? "Unstable lane prediction"
                        : self.rawPrediction == nil ? "Looking for painted lines" : "Lane predictions ready"
                }
                if self.generation == submittedGeneration {
                    if finishedAt - time <= 1.0,
                       let lane, abs(lane.capturedAt - time) < 0.001,
                       let measurement = LaneModelMeasurement(lane: lane, frame: frame) {
                        self.neuralMeasurements.append(measurement)
                    } else {
                        self.neuralMeasurements.invalidate(at: time)
                    }
                }
                let roadModel = self.roadModel
                self.stateLock.unlock()
                if let roadModel {
                    self.scheduleRoad(roadModel, buffer: buffer, frame: frame, lane: lane,
                        excluding: boxes, at: time, generation: submittedGeneration,
                        includeVisibility: arRequested)
                }
                let finished = ProcessInfo.processInfo.systemUptime
                self.consecutiveErrors = 0
                self.stateLock.lock()
                let roadRows = self.roadMeasurements.last?.region?.bands.count ?? 0
                self.stateLock.unlock()
                if finished - self.lastStatusTime >= 2 {
                    self.lastStatusTime = finished
                    let duration = Int((finished - started) * 1000)
                    let count = displayed.map { Set($0.boundaries.map(\.laneSlot)).count } ?? 0
                    self.onStatus?(finished - time > 1.0
                        ? "UFLDv2 · \(duration) ms · frame too old to track"
                        : "UFLDv2 · \(mode.title) · \(duration) ms · \(count) display boundaries · road \(roadRows) rows · \(self.neuralEstimator.status) · lead \(self.leadLaneEstimator.status)")
                }
            } catch {
                self.consecutiveErrors += 1
                self.stateLock.lock()
                guard self.generation == submittedGeneration else { self.stateLock.unlock(); return }
                let retryDelay = self.consecutiveErrors < 3 ? 0.5 : 3.0
                self.lastSubmission = ProcessInfo.processInfo.systemUptime + retryDelay
                self.neuralMeasurements.reset()
                self.leadLaneMeasurements.reset()
                self.leadLaneState = "lane model error"
                self.rawPrediction = nil; self.diagnostic = "Lane model error"
                self.roadMeasurements = []
                self.stateLock.unlock()
                self.neuralEstimator.reset()
                self.leadLaneEstimator.reset()
                self.displaySmoother.reset()
                self.kalmanSmoother.reset()
                self.onStatus?("UFLDv2/Intel road model inference error: \(error.localizedDescription) · retrying · paint fallback")
            }
        }
    }

    /// Road inference is optional and never makes UFLD's one-frame slot wait
    /// for Intel road model prediction or mask decoding. Both still share the vehicle-
    /// first accelerator gate, and an old frame cannot publish after reset.
    private func scheduleRoad(_ roadModel: MLModel, buffer: CVPixelBuffer, frame: LaneLumaFrame,
                              lane: LanePath?, excluding boxes: [CGRect], at time: TimeInterval,
                              generation submittedGeneration: Int, includeVisibility: Bool) {
        stateLock.lock()
        guard generation == submittedGeneration, !roadBusy,
              roadInterval.map({ time - lastRoadSubmission >= $0 }) == true else {
            stateLock.unlock(); return
        }
        roadBusy = true
        stateLock.unlock()
        roadQueue.async {
            defer {
                self.stateLock.lock(); self.roadBusy = false; self.stateLock.unlock()
            }
            self.stateLock.lock(); let current = self.generation == submittedGeneration; self.stateLock.unlock()
            guard current, ProcessInfo.processInfo.systemUptime - time <= 0.40 else { return }
            let started = ProcessInfo.processInfo.systemUptime
            var attempted = false
            var failed = false
            var road: RoadRegion?
            var diagnosticMask: RoadVisibilityMask?
            do {
                let result = try self.inference.auxiliary {
                    self.stateLock.lock(); let current = self.generation == submittedGeneration; self.stateLock.unlock()
                    guard current, ProcessInfo.processInfo.systemUptime - time <= 0.40 else {
                        return nil as MLFeatureProvider?
                    }
                    attempted = true
                    let trace = PerformanceTrace.begin(.roadInference)
                    defer { PerformanceTrace.end(trace) }
                    let input = try self.makeInput(buffer, width: 640, height: 384, pool: &self.roadInputPool)
                    let provider = try MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: input)])
                    return try roadModel.prediction(from: provider)
                }
                if let result = result.flatMap({ $0 }) {
                    guard let scores = result.featureValue(for: "road_scores")?.multiArrayValue else {
                        throw LaneModelError.output
                    }
                    let decoded = try self.decodeRoad(scores, excluding: boxes, at: time,
                        includeVisibility: includeVisibility)
                    road = decoded.region
                    diagnosticMask = decoded.mask
                }
            } catch {
                failed = true
                self.onStatus?("UFLDv2 lanes active - Intel road model retrying: \(error.localizedDescription)")
            }
            guard attempted else { return }
            let finished = ProcessInfo.processInfo.systemUptime
            self.stateLock.lock()
            if self.generation == submittedGeneration {
                self.lastRoadSubmission = failed ? finished + 2 : time
                self.roadLatencyHistory.observe(finished - started)
                // A genuine negative mask invalidates prior road support;
                // skipping this job at the gate does not.
                self.roadMeasurements.removeAll { time - $0.time > 1.2 }
                self.roadMeasurements.append((time, road, diagnosticMask, frame, lane))
                if self.roadMeasurements.count > 6 { self.roadMeasurements.removeFirst() }
            }
            self.stateLock.unlock()
        }
    }

    private func decodeRoad(_ roadScores: MLMultiArray, excluding boxes: [CGRect], at time: TimeInterval,
                            includeVisibility: Bool) throws -> (region: RoadRegion?, mask: RoadVisibilityMask?) {
        let transform = LaneImageTransform(width: 640, height: 384, sourceWidth: 640, sourceHeight: 384)!
        return try self.withScores(roadScores) { values in
            let region = RoadSegmentationScanner.scan(transform: transform, excluding: boxes, at: time) { x, y in
                let value = values(x, y); return (value.background, value.foreground)
            }
            guard includeVisibility else { return (region, nil) }
            let width = 160, height = 96
            var pixels = [UInt8](repeating: 0, count: width * height)
            for y in 0..<height {
                for x in 0..<width {
                    let score = values(x * 4 + 2, y * 4 + 2)
                    if score.foreground.isFinite, score.background.isFinite,
                       (0...1).contains(score.foreground), (0...1).contains(score.background), score.foreground >= 0.70,
                       score.foreground - score.background >= 0.25 { pixels[y * width + x] = 1 }
                }
            }
            let mask = RoadVisibilityMask(width: width, height: height, pixels: pixels)
            let accepted = region.map { RoadRegion(bands: $0.bands, confidence: $0.confidence,
                capturedAt: time, visibility: mask) }
            return (accepted, mask)
        }
    }

    private func makeInput(_ buffer: CVPixelBuffer, width: Int, height: Int, pool: inout CVPixelBufferPool?, laneCrop: Bool = false) throws -> CVPixelBuffer {
        if pool == nil {
            let attributes: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: width,
                kCVPixelBufferHeightKey as String: height,
                kCVPixelBufferIOSurfacePropertiesKey as String: [:]
            ]
            guard CVPixelBufferPoolCreate(kCFAllocatorDefault, nil, attributes as CFDictionary, &pool) == kCVReturnSuccess else {
                throw LaneModelError.frame
            }
        }
        guard let inputPool = pool else { throw LaneModelError.frame }
        var resized: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, inputPool, &resized) == kCVReturnSuccess,
              let resized else { throw LaneModelError.frame }
        let source = CIImage(cvPixelBuffer: buffer)
        let rect = CGRect(x: 0, y: 0, width: width, height: height)
        guard source.extent.width.isFinite, source.extent.height.isFinite,
              source.extent.width > 0, source.extent.height > 0 else { throw LaneModelError.frame }
        // UFLDv2 training resizes its canonical full image to 1600 x 533,
        // then keeps the bottom 320 rows. Core Image's origin is bottom-left,
        // so cropping to rect below removes the top 213 resized rows.
        // Output anchors still refer to the full camera frame, not the crop.
        // The Intel road model takes the same full-frame 640 x 384 RGB stretch and
        // converts to BGR 896 x 512 inside the Core ML graph.
        let resizedHeight = laneCrop ? UFLDv2InputTransform.resizedHeight : height
        let image = source.transformed(by: CGAffineTransform(translationX: -source.extent.minX, y: -source.extent.minY))
            .transformed(by: CGAffineTransform(scaleX: CGFloat(width) / source.extent.width, y: CGFloat(resizedHeight) / source.extent.height))
        context.render(image.cropped(to: rect), to: resized,
                       bounds: rect, colorSpace: CGColorSpaceCreateDeviceRGB())
        return resized
    }

    private func decode(_ result: MLFeatureProvider) throws
        -> (ego: [UFLDv2LaneDecoder.Candidate], display: [UFLDv2LaneDecoder.Candidate]) {
        let rowLocations = try LaneOutputTensor(result, name: "loc_row", shape: [1, 200, 72, 4])
        let columnLocations = try LaneOutputTensor(result, name: "loc_col", shape: [1, 100, 81, 4])
        let rowExistence = try LaneOutputTensor(result, name: "exist_row", shape: [1, 2, 72, 4])
        let columnExistence = try LaneOutputTensor(result, name: "exist_col", shape: [1, 2, 81, 4])
        // Keep trained ego identity through corridor fitting. Decode each head
        // once, then apply separate stock-corridor and display selection gates.
        let location: (UFLDv2LaneDecoder.Axis, Int, Int, Int) -> Double = { axis, bin, anchor, slot in
            (axis == .row ? rowLocations : columnLocations).value(bin, anchor, slot)
        }
        let existence: (UFLDv2LaneDecoder.Axis, Int, Int, Int) -> Double = { axis, state, anchor, slot in
            (axis == .row ? rowExistence : columnExistence).value(state, anchor, slot)
        }
        return UFLDv2LaneDecoder.cameraCandidates(location: location, existence: existence)
    }

    private func withScores<T>(_ array: MLMultiArray,
                               body: ((Int, Int) -> (background: Double, foreground: Double)) -> T) throws -> T {
        let shape = array.shape.map { $0.intValue }
        let strides = array.strides.map { $0.intValue }
        guard shape.count >= 3, Array(shape.suffix(3)) == [2, 384, 640],
              shape.dropLast(3).allSatisfy({ $0 == 1 }) else { throw LaneModelError.output }
        let channels = strides[strides.count - 3]
        let rows = strides[strides.count - 2]
        let columns = strides[strides.count - 1]
        guard channels > 0, rows > 0, columns > 0 else { throw LaneModelError.output }
        switch array.dataType {
        case .float32:
            let pointer = array.dataPointer.assumingMemoryBound(to: Float.self)
            return body { x, y in
                let offset = y * rows + x * columns
                return (Double(pointer[offset]), Double(pointer[channels + offset]))
            }
        case .float16:
            let pointer = array.dataPointer.assumingMemoryBound(to: UInt16.self)
            return body { x, y in
                let offset = y * rows + x * columns
                return (Double(Float16(bitPattern: pointer[offset])), Double(Float16(bitPattern: pointer[channels + offset])))
            }
        case .double:
            let pointer = array.dataPointer.assumingMemoryBound(to: Double.self)
            return body { x, y in
                let offset = y * rows + x * columns
                return (pointer[offset], pointer[channels + offset])
            }
        default: throw LaneModelError.output
        }
    }

    private func lumaFrame(_ buffer: CVPixelBuffer, at time: TimeInterval) -> LaneLumaFrame? {
        let format = CVPixelBufferGetPixelFormatType(buffer)
        let planar = CVPixelBufferGetPlaneCount(buffer) == 2
        let videoRange = format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        let rgb = format == kCVPixelFormatType_32BGRA || format == kCVPixelFormatType_32RGBA
        guard (planar && (videoRange || format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)) || (!planar && rgb),
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let width = planar ? CVPixelBufferGetWidthOfPlane(buffer, 0) : CVPixelBufferGetWidth(buffer)
        let height = planar ? CVPixelBufferGetHeightOfPlane(buffer, 0) : CVPixelBufferGetHeight(buffer)
        let stride = planar ? CVPixelBufferGetBytesPerRowOfPlane(buffer, 0) : CVPixelBufferGetBytesPerRow(buffer)
        guard width > 0, height > 0, stride >= width * (planar ? 1 : 4),
              let base = planar ? CVPixelBufferGetBaseAddressOfPlane(buffer, 0) : CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let targetWidth = 320, targetHeight = max(64, min(640, Int(Double(height) * 320 / Double(width))))
        let source = base.assumingMemoryBound(to: UInt8.self)
        let bgra = format == kCVPixelFormatType_32BGRA
        var pixels = [UInt8](repeating: 0, count: targetWidth * targetHeight)
        for y in 0..<targetHeight {
            let sourceY = min(height - 1, y * height / targetHeight)
            for x in 0..<targetWidth {
                let sourceX = min(width - 1, x * width / targetWidth)
                let offset = sourceY * stride + sourceX * (planar ? 1 : 4)
                if planar {
                    pixels[y * targetWidth + x] = videoRange ? FramePixelMath.fullRangeLuma(source[offset]) : source[offset]
                } else {
                    pixels[y * targetWidth + x] = FramePixelMath.luma(red: source[offset + (bgra ? 2 : 0)],
                        green: source[offset + 1], blue: source[offset + (bgra ? 0 : 2)])
                }
            }
        }
        return LaneLumaFrame(width: targetWidth, height: targetHeight, pixels: pixels, capturedAt: time)
    }

    private func locateModel(named name: String) throws -> URL {
        for bundle in [Bundle.module, Bundle.main] + Bundle.allBundles + Bundle.allFrameworks {
            if let source = bundle.url(forResource: "LaneModelData", withExtension: nil) {
                let model = source.appendingPathComponent("\(name).mlpackage")
                if FileManager.default.fileExists(atPath: model.path) { return model }
            }
            if let source = bundle.resourceURL?.appendingPathComponent("LaneModelData/\(name).mlpackage"),
               FileManager.default.fileExists(atPath: source.path) { return source }
            if let source = bundle.url(forResource: name, withExtension: "mlpackage") { return source }
        }
        throw LaneModelError.missing
    }

    private func compiledModel(from source: URL) throws -> URL {
        // A content hash invalidates the on-device cache when model weights
        // change, while keeping first-launch compilation out of later trips.
        guard let enumerator = FileManager.default.enumerator(at: source, includingPropertiesForKeys: [.isRegularFileKey]) else { throw LaneModelError.missing }
        let files = enumerator.compactMap { $0 as? URL }.sorted { $0.path < $1.path }
        var hasher = SHA256()
        for file in files where (try file.resourceValues(forKeys: [.isRegularFileKey])).isRegularFile == true {
            hasher.update(data: Data(file.path.dropFirst(source.path.count).utf8))
            hasher.update(data: try Data(contentsOf: file))
        }
        let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        let directory = try FileManager.default.url(for: .cachesDirectory, in: .userDomainMask,
                                                    appropriateFor: nil, create: true)
            .appendingPathComponent("BrakeCheckLaneModels", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent("\(source.deletingPathExtension().lastPathComponent)-\(digest.prefix(16)).mlmodelc", isDirectory: true)
        if FileManager.default.fileExists(atPath: destination.path) { return destination }
        let compiled = try MLModel.compileModel(at: source)
        try FileManager.default.copyItem(at: compiled, to: destination)
        return destination
    }

    private func scanMarkings(_ buffer: CVPixelBuffer, excluding boxes: [CGRect], at time: TimeInterval) -> LanePath? {
        guard time - lastScan >= 0.18 else {
            return latest.flatMap { $0.isUsable(at: time) ? $0 : nil }
        }
        lastScan = time
        let format = CVPixelBufferGetPixelFormatType(buffer)
        let planar = CVPixelBufferGetPlaneCount(buffer) == 2
        let videoRange = format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        let rgb = format == kCVPixelFormatType_32BGRA || format == kCVPixelFormatType_32RGBA
        guard (planar && (videoRange || format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)) || (!planar && rgb),
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
            latest = nil; estimator.reset(); return nil
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        let rows: [LaneMarkingRow]
        if planar {
            guard let yBase = CVPixelBufferGetBaseAddressOfPlane(buffer, 0),
                  let uvBase = CVPixelBufferGetBaseAddressOfPlane(buffer, 1) else { latest = nil; return nil }
            let yStride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
            let uvStride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 1)
            guard width > 0, height > 0, yStride >= width, uvStride >= ((width + 1) / 2) * 2 else { latest = nil; return nil }
            let yBytes = yBase.assumingMemoryBound(to: UInt8.self)
            let uvBytes = uvBase.assumingMemoryBound(to: UInt8.self)
            rows = LaneMarkingScanner.scan(width: width, height: height, excluding: boxes,
                luma: { x, y in
                    let value = yBytes[y * yStride + x]
                    return videoRange ? FramePixelMath.fullRangeLuma(value) : value
                }, chroma: { x, y in
                    let offset = (y / 2) * uvStride + (x / 2) * 2
                    let cb = uvBytes[offset], cr = uvBytes[offset + 1]
                    return videoRange ? (FramePixelMath.fullRangeChroma(cb), FramePixelMath.fullRangeChroma(cr)) : (cb, cr)
                })
        } else {
            guard let base = CVPixelBufferGetBaseAddress(buffer) else { latest = nil; return nil }
            let stride = CVPixelBufferGetBytesPerRow(buffer)
            guard width > 0, height > 0, stride >= width * 4 else { latest = nil; return nil }
            let bytes = base.assumingMemoryBound(to: UInt8.self), bgra = format == kCVPixelFormatType_32BGRA
            rows = LaneMarkingScanner.scan(width: width, height: height, excluding: boxes,
                luma: { x, y in
                    let offset = y * stride + x * 4
                    return FramePixelMath.luma(red: bytes[offset + (bgra ? 2 : 0)],
                        green: bytes[offset + 1], blue: bytes[offset + (bgra ? 0 : 2)])
                }, chroma: { x, y in
                    let offset = y * stride + x * 4
                    return FramePixelMath.chroma(red: bytes[offset + (bgra ? 2 : 0)],
                        green: bytes[offset + 1], blue: bytes[offset + (bgra ? 0 : 2)])
                })
        }
        latest = estimator.update(rows, at: time)
        return latest.flatMap { $0.isUsable(at: time) ? $0 : nil }
    }
}

private enum LaneModelError: LocalizedError {
    case missing, contract, frame, output
    var errorDescription: String? {
        switch self {
        case .missing: return "UFLDv2Lane or IntelRoad model package is missing"
        case .contract: return "expected UFLDv2 1600×320 four-head outputs and IntelRoad 640×384 road_scores"
        case .frame: return "camera frame could not be resized"
        case .output: return "unexpected floating-point lane or road output contract"
        }
    }
}


/// Retains each tensor while reading its actual Core ML layout without NSNumber
/// allocations. Non-contiguous positive strides and FP16/FP32/FP64 are supported.
private struct LaneOutputTensor {
    private let array: MLMultiArray
    private let strides: [Int]
    private let read: (Int) -> Double

    init(_ result: MLFeatureProvider, name: String, shape: [Int]) throws {
        guard let array = result.featureValue(for: name)?.multiArrayValue,
              array.shape.map({ $0.intValue }) == shape else { throw LaneModelError.output }
        let strides = array.strides.map { $0.intValue }
        guard strides.count == 4, strides.allSatisfy({ $0 > 0 }) else { throw LaneModelError.output }
        self.array = array
        self.strides = strides
        switch array.dataType {
        case .float32:
            let values = array.dataPointer.assumingMemoryBound(to: Float.self)
            read = { Double(values[$0]) }
        case .float16:
            let values = array.dataPointer.assumingMemoryBound(to: UInt16.self)
            read = { Double(Float16(bitPattern: values[$0])) }
        case .double:
            let values = array.dataPointer.assumingMemoryBound(to: Double.self)
            read = { values[$0] }
        default: throw LaneModelError.output
        }
    }

    func value(_ bin: Int, _ anchor: Int, _ slot: Int) -> Double {
        withExtendedLifetime(array) {
            read(bin * strides[1] + anchor * strides[2] + slot * strides[3])
        }
    }
}
