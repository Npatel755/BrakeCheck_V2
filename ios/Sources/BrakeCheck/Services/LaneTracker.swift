import BrakeCheckCore
import CoreGraphics
import CoreImage
import CoreML
import CoreVideo
import CryptoKit
import Foundation

/// The marking fallback runs on VehicleTracker's queue. Core ML has a separate
/// queue with at most one retained camera frame, so lane inference cannot build
/// up a backlog. The shared prediction gate gives vehicle requests priority. The model is bundled; no network/key is required.
final class LaneTracker {
    var onStatus: ((String) -> Void)?
    private let modelQueue = DispatchQueue(label: "BrakeCheck.lane-model", qos: .utility,
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
    private var generation = 0
    private var lastSubmission: TimeInterval = -.infinity
    private var neuralMeasurements = LaneMeasurementBuffer()
    private var roadMeasurements: [(time: TimeInterval, region: RoadRegion?, frame: LaneLumaFrame, lane: LanePath?)] = []
    private var vehicleBoxes: [CGRect] = []
    private var boxesTime: TimeInterval = -.infinity

    // These properties are used exclusively on modelQueue.
    private var model: MLModel?
    private var roadModel: MLModel?
    private var neuralEstimator = NeuralLaneEstimator()
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var inputPool: CVPixelBufferPool?
    private var lastStatusTime: TimeInterval = -.infinity
    private var consecutiveErrors = 0
    private var inputWidth = 800
    private var inputHeight = 288
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
    private var lastProjectedRoad: RoadRegion?
    private var projectedRoadSource: TimeInterval = -.infinity

    func prepare() {
        guard AppScope.neuralLanes else {
            onStatus?("Lane paint scanner (trained lane models off in this build)")
            return
        }
        stateLock.lock()
        guard !loadingStarted else { stateLock.unlock(); return }
        loadingStarted = true
        stateLock.unlock()
        modelQueue.async {
            self.onStatus?("UFLD + SegFormer loading…")
            do {
                let source = try self.locateModel(named: "UFLDLane")
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
                guard let image = model.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
                      image.pixelsWide == 800, image.pixelsHigh == 288,
                      model.modelDescription.outputDescriptionsByName["lane_logits"]?.multiArrayConstraint?.shape.map({ $0.intValue }) == [1, 201, 18, 4] else {
                    throw LaneModelError.contract
                }
                self.inputWidth = image.pixelsWide; self.inputHeight = image.pixelsHigh
                self.model = model
                self.stateLock.lock(); self.modelReady = true; self.stateLock.unlock()
                self.onStatus?("UFLD lanes ready · road model loading")
                do {
                    let roadSource = try self.locateModel(named: "SegFormerRoad")
                    let compiled = try self.compiledModel(from: roadSource)
                    let road: MLModel
                    do { road = try MLModel(contentsOf: compiled, configuration: options) }
                    catch {
                        try FileManager.default.removeItem(at: compiled)
                        road = try MLModel(contentsOf: self.compiledModel(from: roadSource), configuration: options)
                    }
                    guard let roadImage = road.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
                          roadImage.pixelsWide == 640, roadImage.pixelsHigh == 384,
                          road.modelDescription.outputDescriptionsByName["road_scores"]?.multiArrayConstraint?.shape.map({ $0.intValue }) == [1, 2, 384, 640] else { throw LaneModelError.contract }
                    self.roadModel = road
                    self.onStatus?("UFLD lanes + SegFormer road ready")
                } catch {
                    self.roadModel = nil
                    self.onStatus?("UFLD lanes ready · road segmentation unavailable: \(error.localizedDescription)")
                }
            } catch {
                self.stateLock.lock(); self.loadingStarted = false; self.stateLock.unlock()
                self.onStatus?("UFLD/SegFormer unavailable: \(error.localizedDescription) · paint fallback")
            }
        }
    }

    func reset() {
        estimator.reset(); latest = nil; roadRegion = nil; arScene = nil; lastScan = -.infinity
        arSceneTracker.reset()
        lastVisualUpdate = -.infinity; lastTrackedLane = nil; lastProjectedRoad = nil
        stateLock.lock()
        generation += 1
        neuralMeasurements.reset()
        roadMeasurements = []
        vehicleBoxes = []; boxesTime = -.infinity
        lastSubmission = -.infinity
        lastRoadSubmission = -.infinity
        // Keep modelBusy true until an in-flight old prediction releases its
        // frame. Its generation prevents it from publishing into a new trip.
        stateLock.unlock()
        modelQueue.async { self.neuralEstimator.reset() }
    }

    func update(_ buffer: CVPixelBuffer, excluding boxes: [CGRect], at time: TimeInterval) -> LanePath? {
        let now = max(time, ProcessInfo.processInfo.systemUptime)
        stateLock.lock()
        vehicleBoxes = boxes; boxesTime = time
        let measurement = neuralMeasurements.latest(at: time)
        let roadObservation = roadMeasurements.last { $0.time <= time }
        let road = roadObservation?.region
        let arEnabled = arProcessingEnabled
        stateLock.unlock()
        roadRegion = road.flatMap { $0.isUsable(at: now) ? $0 : nil }
        let visualDue = time - lastVisualUpdate >= 0.19
        if visualDue { lastVisualUpdate = time }
        let needsFrame = measurement != nil || (arEnabled && roadObservation?.lane != nil)
        let frame = visualDue && needsFrame ? lumaFrame(buffer, at: time) : nil
        var tracked: LanePath?
        if let measurement, let frame {
            tracked = LaneVisualTracker.project(measurement.lane, from: measurement.frame, to: frame, excluding: boxes)
            if let tracked { lastTrackedLane = tracked }
        } else if !visualDue {
            tracked = lastTrackedLane.flatMap { $0.isUsable(at: now) && time - $0.capturedAt <= 0.25 ? $0 : nil }
        }
        var arRoad = roadRegion
        if arEnabled, let observation = roadObservation, let road = observation.region,
           time - observation.time > 0.02, time - observation.time <= 0.8,
           let originalLane = observation.lane, let frame {
            let sameSource = observation.frame.capturedAt == measurement?.frame.capturedAt
            let trackedLane = sameSource ? tracked
                : LaneVisualTracker.project(originalLane, from: observation.frame, to: frame, excluding: boxes)
            // AR-only visual prediction. Never feed a warped road into safety
            // eligibility, and always warp back to the original inference.
            arRoad = trackedLane.flatMap { RoadVisualProjection.project(road, from: originalLane, to: $0, at: time) }
            lastProjectedRoad = arRoad; projectedRoadSource = observation.time
        } else if arEnabled, !visualDue, projectedRoadSource == roadObservation?.time,
                  let cached = lastProjectedRoad, cached.isUsable(at: now) {
            arRoad = cached
        } else if let observation = roadObservation, time - observation.time > 0.35 {
            arRoad = nil
        }
        func publish(_ lane: LanePath?, arLane: LanePath? = nil) -> LanePath? {
            guard arEnabled else { return lane }
            // AR uses the current visually tracked neural corridor directly;
            // collision lane fusion retains its original timestamps/behavior.
            let scene = CameraARScene(lane: arLane ?? lane, road: arRoad,
                laneObservedAt: lane?.source == .markings ? (lane?.capturedAt ?? -.infinity)
                    : (measurement?.lane.capturedAt ?? lane?.capturedAt ?? -.infinity),
                roadObservedAt: roadObservation?.time ?? -.infinity, capturedAt: time)
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
            guard ProcessInfo.processInfo.systemUptime - time <= 0.30 else { return }
            let started = ProcessInfo.processInfo.systemUptime
            do {
                guard let prediction = try self.inference.auxiliary({
                    let input = try self.makeInput(buffer, width: 800, height: 288, pool: &self.inputPool)
                    guard let frame = self.lumaFrame(buffer, at: time) else { throw LaneModelError.frame }
                    let provider = try MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: input)])
                    return (try model.prediction(from: provider), frame)
                }) else {
                    // No waiting frame: retry on a newer capture soon, rather
                    // than spending the entire lane interval on a dropped job.
                    self.stateLock.lock()
                    if self.generation == submittedGeneration, self.lastSubmission == time {
                        self.lastSubmission = time - self.minimumInterval + 0.05
                    }
                    self.stateLock.unlock()
                    return
                }
                let (result, frame) = prediction
                self.stateLock.lock()
                let laneDuration = ProcessInfo.processInfo.systemUptime - started
                self.laneLatencyHistory.observe(laneDuration)
                let roadDue = self.roadInterval.map { time - self.lastRoadSubmission >= $0 } ?? false
                self.stateLock.unlock()
                guard let scores = result.featureValue(for: "lane_logits")?.multiArrayValue else {
                    throw LaneModelError.output
                }
                // Decode and publish lanes before waiting for segmentation.
                // Road-model failure/cadence must not suppress UFLD observations.
                let rows = try self.decode(scores, excluding: boxes)
                let lane = self.neuralEstimator.update(rows, at: time)
                self.stateLock.lock()
                if self.generation == submittedGeneration,
                   ProcessInfo.processInfo.systemUptime - time <= 1.0,
                   let lane, abs(lane.capturedAt - time) < 0.001,
                   let measurement = LaneModelMeasurement(lane: lane, frame: frame) {
                    self.neuralMeasurements.append(measurement)
                }
                self.stateLock.unlock()
                var roadAttempted = false
                var roadFailed = false
                var road: RoadRegion?
                let roadStarted = ProcessInfo.processInfo.systemUptime
                do {
                    let result = try self.inference.auxiliary {
                        guard roadDue, let roadModel = self.roadModel else { return nil as MLFeatureProvider? }
                        guard ProcessInfo.processInfo.systemUptime - time <= 0.40 else { return nil as MLFeatureProvider? }
                        roadAttempted = true
                        let input = try self.makeInput(buffer, width: 640, height: 384, pool: &self.roadInputPool)
                        let provider = try MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: input)])
                        return try roadModel.prediction(from: provider)
                    }
                    if let result = result.flatMap({ $0 }) {
                        guard let scores = result.featureValue(for: "road_scores")?.multiArrayValue else { throw LaneModelError.output }
                        road = try self.decodeRoad(scores, excluding: boxes, at: time, includeVisibility: arRequested)
                    }
                } catch {
                    roadFailed = true
                    self.onStatus?("UFLD lanes active - SegFormer retrying: \(error.localizedDescription)")
                }
                let finished = ProcessInfo.processInfo.systemUptime
                self.consecutiveErrors = 0
                self.stateLock.lock()
                if self.generation == submittedGeneration {
                    // Keep nil observations too: a failed current mask cannot
                    // silently revive an older road measurement.
                    self.roadMeasurements.removeAll { time - $0.time > 1.2 }
                    if roadAttempted {
                        self.lastRoadSubmission = roadFailed ? ProcessInfo.processInfo.systemUptime + 2 : time
                        let duration = ProcessInfo.processInfo.systemUptime - roadStarted
                        self.roadLatencyHistory.observe(duration)
                        // A genuine negative prediction invalidates history;
                        // cadence skips retain only bounded visual support.
                        self.roadMeasurements.append((time, road, frame, lane))
                    }
                    if self.roadMeasurements.count > 6 { self.roadMeasurements.removeFirst() }
                }
                self.stateLock.unlock()
                if finished - self.lastStatusTime >= 2 {
                    self.lastStatusTime = finished
                    let duration = Int((finished - started) * 1000)
                    let count = rows.filter { !$0.peaks.isEmpty }.count
                    self.onStatus?(finished - time > 1.0
                        ? "UFLD + SegFormer · \(duration) ms · frame too old to track"
                        : "UFLD + SegFormer · \(duration) ms · \(count) lane rows · road \(road?.bands.count ?? 0) rows · \(self.neuralEstimator.status)")
                }
            } catch {
                self.consecutiveErrors += 1
                self.stateLock.lock()
                guard self.generation == submittedGeneration else { self.stateLock.unlock(); return }
                let retryDelay = self.consecutiveErrors < 3 ? 0.5 : 3.0
                self.lastSubmission = ProcessInfo.processInfo.systemUptime + retryDelay
                self.neuralMeasurements.reset()
                self.roadMeasurements = []
                self.stateLock.unlock()
                self.neuralEstimator.reset()
                self.onStatus?("UFLD/SegFormer inference error: \(error.localizedDescription) · retrying · paint fallback")
            }
        }
    }

    private func decodeRoad(_ roadScores: MLMultiArray, excluding boxes: [CGRect], at time: TimeInterval, includeVisibility: Bool) throws -> RoadRegion? {
        let transform = LaneImageTransform(width: 640, height: 384, sourceWidth: 640, sourceHeight: 384)!
        return try self.withScores(roadScores) { values in
            let region = RoadSegmentationScanner.scan(transform: transform, excluding: boxes, at: time) { x, y in
                let value = values(x, y); return (value.background, value.foreground)
            }
            guard let region else { return nil as RoadRegion? }
            guard includeVisibility else { return region }
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
            return RoadRegion(bands: region.bands, confidence: region.confidence, capturedAt: time,
                visibility: RoadVisibilityMask(width: width, height: height, pixels: pixels))
        }
    }

    private func makeInput(_ buffer: CVPixelBuffer, width: Int, height: Int, pool: inout CVPixelBufferPool?) throws -> CVPixelBuffer {
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
        // Both checkpoints were trained with a full-frame stretch, not YOLO
        // letterboxing. Normalize RGB inside the exported model graphs.
        let image = source.transformed(by: CGAffineTransform(translationX: -source.extent.minX, y: -source.extent.minY))
            .transformed(by: CGAffineTransform(scaleX: CGFloat(width) / source.extent.width, y: CGFloat(height) / source.extent.height))
        context.render(image.cropped(to: rect), to: resized,
                       bounds: rect, colorSpace: CGColorSpaceCreateDeviceRGB())
        return resized
    }

    private func decode(_ array: MLMultiArray, excluding boxes: [CGRect]) throws -> [LaneMarkingRow] {
        guard array.shape.map({ $0.intValue }) == [1, 201, 18, 4] else { throw LaneModelError.output }
        let strides = array.strides.map { $0.intValue }
        guard strides.count == 4, strides.allSatisfy({ $0 > 0 }) else { throw LaneModelError.output }
        func scan(_ read: (Int) -> Double) -> [LaneMarkingRow] {
            UFLDLaneDecoder.decode(excluding: boxes) { grid, row, lane in
                read(grid * strides[1] + row * strides[2] + lane * strides[3])
            }
        }
        // Avoid ~72,000 temporary NSNumber objects per prediction. Honor the
        // actual Core ML strides and element type, including non-contiguous
        // layouts, just as the road-head reader already does.
        switch array.dataType {
        case .float32:
            let values = array.dataPointer.assumingMemoryBound(to: Float.self)
            return scan { Double(values[$0]) }
        case .float16:
            let values = array.dataPointer.assumingMemoryBound(to: UInt16.self)
            return scan { Double(Float16(bitPattern: values[$0])) }
        case .double:
            let values = array.dataPointer.assumingMemoryBound(to: Double.self)
            return scan { values[$0] }
        default: throw LaneModelError.output
        }
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
        case .missing: return "UFLDLane or SegFormerRoad model package is missing"
        case .contract: return "expected UFLD 800×288 lane_logits and SegFormer 640×384 road_scores"
        case .frame: return "camera frame could not be resized"
        case .output: return "expected floating-point scores [1,2,height,width]"
        }
    }
}
