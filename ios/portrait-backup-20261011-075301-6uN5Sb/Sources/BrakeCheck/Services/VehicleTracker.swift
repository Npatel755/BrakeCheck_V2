import Foundation
import CoreGraphics
import Vision
import CoreML
import CoreVideo
import ImageIO
import CryptoKit
import BrakeCheckCore
struct VehicleDetection: Identifiable {
    let id: UUID
    let box: CGRect
    let label: String
    let confidence: Double
    var viewpoint: VehicleViewpoint? = nil
    var capturedAt: TimeInterval = 0
}
struct TrackingReading {
    let targetID: UUID
    let box: CGRect
    let distance: Double?
    let confidence: Double
    let uptime: TimeInterval
    let rangeSource: String
    var isFreshDetection = true
    var isPathSupported = true
    var isCollisionHazard = false
    var isCameraProjectedPath = false
    var hasConfirmedRearIdentity = false
    var isRearConfirmationRetained = false
    var approachClosingSpeed: Double? = nil
    var canEstimateLeadSpeed = false
    var detectorGeneration = 0
    var isDetectorSettling = false
}
/// Keeps Vision state on one serial queue and drops frames if inference is busy.
final class VehicleTracker {
    private let queue = DispatchQueue(label: "BrakeCheck.vision", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false
    private var frameGeneration = 0
    private var detector: VNCoreMLRequest?
    private var standardDetector: VNCoreMLRequest?
    private var lowLightDetector: VNCoreMLRequest?
    private var lighting = DetectorLightingSelector()
    private var activeProfile: DetectorLightingProfile = .day
    private var detectorGeneration = 0
    private var detectorSettlingUntil: TimeInterval = -.infinity
    private var lowLightError: String?
    private var lastLightingReport: TimeInterval = -.infinity
    private var prepared = false
    private var unsupportedOutputReported = false
    private var vehicleStatus = "YOLO waiting"
    private var laneStatus = "YOLOP lanes waiting"
    private var headingStatus = "Vehicle front/rear model waiting"
    private var leadStatus = "Road + motion confirmation ready"
    private var lastLeadReport: TimeInterval = -.infinity
    private var lastInferenceReport: TimeInterval = -.infinity
    private var manualRequest: VNTrackObjectRequest?
    private var sequence = VNSequenceRequestHandler()
    private var targetID: UUID?
    private var previousBox: CGRect?
    private var automaticSelector = LeadSelector(requiresRearView: true)
    private var hazardSelector = VehicleHazardSelector()
    private let laneTracker = LaneTracker()
    private let headingTracker = VehicleHeadingTracker()
    private var motionGuard = VehicleMotionGuard()
    private var egoSamples: [GPSSpeedSample] = []
    private var automaticRequest: VNTrackObjectRequest?
    private var automaticSequence = VNSequenceRequestHandler()
    var onLane: ((LanePath?) -> Void)?
    var onRoad: ((RoadRegion?) -> Void)?
    private var referenceDistance = 0.0
    private var referenceWidth = 0.0
    private var horizontalFOV: Double?
    /// Existing callers can continue to assign these callbacks.
    var onReading: ((TrackingReading) -> Void)?
    var onLost: ((UUID, String) -> Void)?
    var onDetections: (([VehicleDetection], TimeInterval) -> Void)?
    var onStatus: ((String) -> Void)?
    var onLeadStatus: ((String) -> Void)?
    var onLightingStatus: ((String) -> Void)?
    init() {
        laneTracker.onStatus = { [weak self] status in
            guard let self else { return }
            self.queue.async {
                self.laneStatus = status
                self.onStatus?("\(self.vehicleStatus) · \(status) · \(self.headingStatus) · \(self.leadStatus)")
                print("[BrakeCheck lanes] \(status)")
            }
        }
        headingTracker.onStatus = { [weak self] status in
            guard let self else { return }
            self.queue.async {
                self.headingStatus = status
                self.onStatus?("\(self.vehicleStatus) · \(self.laneStatus) · \(status) · \(self.leadStatus)")
                print("[BrakeCheck viewpoint] \(status)")
            }
        }
    }
    /// Runs model loading off the camera callback thread.
    func prepare() {
        queue.async {
            self.laneTracker.prepare()
            self.headingTracker.prepare()
            guard !self.prepared else { return }
            self.prepared = true
            self.report("Loading YOLO Core ML model...")
            do {
                let request = try self.makeDetector(from: self.locateModel(), name: "standard")
                self.standardDetector = request
                self.detector = request
                self.report("Standard YOLO ready · loading low-light detector…")
                do {
                    self.lowLightDetector = try self.makeDetector(from: self.locateLowLightModel(), name: "night")
                    self.lowLightError = nil
                    self.report("N5 · standard and low-light YOLO11n ready")
                } catch {
                    self.lowLightError = error.localizedDescription
                    self.report("Standard YOLO ready · low-light fallback: \(error.localizedDescription)")
                }
                self.publishLighting(at: ProcessInfo.processInfo.systemUptime, force: true)
            } catch {
                self.prepared = false // allow a later retry
                self.detector = nil
                self.report("YOLO model could not load: \(error.localizedDescription)")
            }
        }
    }

    func setLightingMode(_ mode: DetectorLightingMode) {
        queue.async {
            self.lighting.setMode(mode)
            self.lastLightingReport = -.infinity
            self.publishLighting(at: ProcessInfo.processInfo.systemUptime, force: true)
        }
    }

    private func makeDetector(from source: URL, name: String) throws -> VNCoreMLRequest {
        let options = MLModelConfiguration(); options.computeUnits = .all
        var compiled = try compiledModel(from: source, name: name)
        let model: MLModel
        do { model = try MLModel(contentsOf: compiled, configuration: options) }
        catch {
            guard source.pathExtension.lowercased() != "mlmodelc" else { throw error }
            try? FileManager.default.removeItem(at: compiled)
            compiled = try compiledModel(from: source, name: name)
            model = try MLModel(contentsOf: compiled, configuration: options)
        }
        guard let image = model.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
              image.pixelsWide == 640, image.pixelsHigh == 640,
              model.modelDescription.outputDescriptionsByName["confidence"] != nil,
              model.modelDescription.outputDescriptionsByName["coordinates"] != nil else {
            throw TrackerError.modelContract
        }
        let request = VNCoreMLRequest(model: try VNCoreMLModel(for: model))
        request.imageCropAndScaleOption = .scaleFit
        // Warm each detector sequentially on this queue. Discard these results;
        // only one vehicle detector performs inference on each live frame.
        var buffer: CVPixelBuffer?
        if CVPixelBufferCreate(kCFAllocatorDefault, 640, 640, kCVPixelFormatType_32BGRA,
                               nil, &buffer) == kCVReturnSuccess, let buffer {
            if CVPixelBufferLockBaseAddress(buffer, []) == kCVReturnSuccess {
                if let base = CVPixelBufferGetBaseAddress(buffer) {
                    memset(base, 127, CVPixelBufferGetBytesPerRow(buffer) * CVPixelBufferGetHeight(buffer))
                }
                CVPixelBufferUnlockBaseAddress(buffer, [])
            }
            try VNImageRequestHandler(cvPixelBuffer: buffer, orientation: .up).perform([request])
        }
        return request
    }

    private func compiledModel(from source: URL, name: String) throws -> URL {
        if source.pathExtension.lowercased() == "mlmodelc" { return source }
        let isPackage = FileManager.default.fileExists(atPath: source.appendingPathComponent("Manifest.json").path)
        let parts = isPackage ? [source.appendingPathComponent("Manifest.json"),
            source.appendingPathComponent("Data/com.apple.CoreML/model.mlmodel"),
            source.appendingPathComponent("Data/com.apple.CoreML/weights/weight.bin")] : [source]
        var digest = SHA256()
        for part in parts {
            let handle = try FileHandle(forReadingFrom: part)
            defer { try? handle.close() }
            while let data = try handle.read(upToCount: 65_536), !data.isEmpty { digest.update(data: data) }
        }
        let hash = digest.finalize().map { String(format: "%02x", $0) }.joined()
        let folder = try FileManager.default.url(for: .cachesDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true).appendingPathComponent("BrakeCheckDetectors", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let destination = folder.appendingPathComponent("\(name)-\(hash).mlmodelc", isDirectory: true)
        if FileManager.default.fileExists(atPath: destination.path) { return destination }
        let copied = isPackage && source.pathExtension.lowercased() != "mlpackage"
        let runtime = copied ? try copyPackageForRuntime(from: source) : source
        defer { if copied { try? FileManager.default.removeItem(at: runtime) } }
        let compiled = try MLModel.compileModel(at: runtime)
        defer { try? FileManager.default.removeItem(at: compiled) }
        let staged = folder.appendingPathComponent("stage-\(UUID().uuidString).mlmodelc", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: staged) }
        try FileManager.default.copyItem(at: compiled, to: staged)
        try FileManager.default.moveItem(at: staged, to: destination)
        return destination
    }
    /// Locates the SwiftPM copied model. The Linux build stores the package
    /// under YOLOModelData to prevent SwiftPM from invoking Apple's coremlc.
    /// Core ML needs an .mlpackage suffix at runtime, so copy the directory.
    private func locateModel() throws -> URL {
        let bundles = [Bundle.module, Bundle.main]
            + Bundle.allBundles + Bundle.allFrameworks
        for bundle in bundles {
            for name in ["yolo11n", "yolov11n"] {
                for ext in ["mlmodelc", "mlpackage", "mlmodel"] {
                    if let url = bundle.url(forResource: name, withExtension: ext) {
                        return url
                    }
                }
            }
            if let source = bundle.url(forResource: "YOLOModelData", withExtension: nil),
               FileManager.default.fileExists(atPath: source.appendingPathComponent("Manifest.json").path) {
                return source
            }
            // Some SwiftPM/xTool bundles expose copied resources via resourceURL.
            if let source = bundle.resourceURL?.appendingPathComponent("YOLOModelData"),
               FileManager.default.fileExists(atPath: source.appendingPathComponent("Manifest.json").path) {
                return source
            }
        }
        throw TrackerError.modelMissing
    }
    private func locateLowLightModel() throws -> URL {
        for bundle in [Bundle.module, Bundle.main] + Bundle.allBundles + Bundle.allFrameworks {
            let roots = [bundle.url(forResource: "YOLOModelData", withExtension: nil),
                         bundle.resourceURL?.appendingPathComponent("YOLOModelData")]
            for root in roots.compactMap({ $0 }) {
                let source = root.appendingPathComponent("Night", isDirectory: true)
                if FileManager.default.fileExists(atPath: source.appendingPathComponent("Manifest.json").path) { return source }
            }
        }
        throw TrackerError.nightModelMissing
    }
    private func copyPackageForRuntime(from source: URL) throws -> URL {
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("BrakeCheckYOLO-\(UUID().uuidString).mlpackage", isDirectory: true)
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        do {
            // The day resource also contains the Night package. Copy only the
            // manifest and model data needed for this individual compilation.
            for part in ["Manifest.json", "Data"] {
                try FileManager.default.copyItem(at: source.appendingPathComponent(part),
                    to: destination.appendingPathComponent(part))
            }
        } catch {
            try? FileManager.default.removeItem(at: destination)
            throw error
        }
        return destination
    }
    func setHorizontalFieldOfView(_ degrees: Double) {
        queue.async {
            self.horizontalFOV = (20...120).contains(degrees) ? degrees : nil
        }
    }
    /// Invalidates in-flight inference before changing the camera axis or replay clip.
    func configureFrameGeometry(horizontalFOV: Double?, completion: (() -> Void)? = nil) {
        lock.lock()
        frameGeneration += 1
        let generation = frameGeneration
        lock.unlock()
        queue.async {
            guard self.isCurrentFrame(generation) else { completion?(); return }
            let oldID = self.targetID
            self.resetTarget()
            self.horizontalFOV = horizontalFOV.flatMap { $0.isFinite && (20...120).contains($0) ? $0 : nil }
            if let oldID { self.onLost?(oldID, "Video geometry changed; reacquiring the lead") }
            completion?()
        }
    }

    private func isCurrentFrame(_ generation: Int) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return generation == frameGeneration
    }

    func updateEgoSpeed(_ sample: GPSSpeedSample?) {
        queue.async {
            guard let sample, sample.measurementUptime.isFinite else { self.egoSamples = []; return }
            guard self.egoSamples.last.map({ sample.measurementUptime > $0.measurementUptime }) ?? true else { return }
            self.egoSamples.removeAll { sample.measurementUptime - $0.measurementUptime > 3 }
            self.egoSamples.append(sample)
            if self.egoSamples.count > 12 { self.egoSamples.removeFirst() }
        }
    }
    func select(box: CGRect, knownDistance: Double, id: UUID) {
        queue.async {
            guard self.usable(box), knownDistance.isFinite, knownDistance > 0 else {
                self.report("Invalid vehicle calibration.")
                return
            }
            self.resetTarget()
            self.targetID = id
            self.previousBox = box
            self.referenceDistance = knownDistance
            self.referenceWidth = Double(box.width)
            let observation = VNDetectedObjectObservation(boundingBox: box)
            let request = VNTrackObjectRequest(detectedObjectObservation: observation)
            request.trackingLevel = .accurate
            self.manualRequest = request
        }
    }
    func clear() {
        queue.async { self.resetTarget() }
    }
    private func resetTarget() {
        lighting.resetSamples()
        manualRequest = nil
        automaticSelector.reset()
        hazardSelector.reset()
        laneTracker.reset()
        headingTracker.reset()
        motionGuard.reset()
        egoSamples = []
        automaticRequest = nil
        automaticSequence = VNSequenceRequestHandler()
        onLane?(nil)
        onRoad?(nil)
        targetID = nil
        previousBox = nil
        referenceDistance = 0
        referenceWidth = 0
        sequence = VNSequenceRequestHandler()
    }
    func submit(_ buffer: CVPixelBuffer, uptime: TimeInterval,
                captureISO: Double? = nil, exposureSeconds: Double? = nil) {
        // Submit before the vehicle busy gate: a slow YOLO frame must not
        // starve lane inference or add its latency to the lane capture age.
        laneTracker.submit(buffer, at: uptime)
        lock.lock()
        guard !busy else {
            lock.unlock()
            return
        }
        busy = true
        let submittedGeneration = frameGeneration
        lock.unlock()
        queue.async {
            defer {
                self.lock.lock()
                self.busy = false
                self.lock.unlock()
            }
            guard self.isCurrentFrame(submittedGeneration) else { return }
            let started = ProcessInfo.processInfo.systemUptime
            do {
                self.updateDetectorLighting(buffer, at: uptime, iso: captureISO, exposure: exposureSeconds)
                var vehicles: [VehicleDetection] = []
                if let detector = self.detector {
                    // CameraService and replay supply physically upright buffers in every orientation.
                    try VNImageRequestHandler(cvPixelBuffer: buffer,
                                              orientation: .up).perform([detector])
                    if let objects = detector.results as? [VNRecognizedObjectObservation] {
                        vehicles = objects.compactMap { observation in
                            guard let top = observation.labels.first else { return nil }
                            let label = top.identifier.lowercased()
                            guard ["car", "truck", "bus", "motorcycle", "motorbike"].contains(label),
                                  top.confidence >= 0.28 else { return nil }
                            return VehicleDetection(id: observation.uuid,
                                                    box: observation.boundingBox,
                                                    label: label,
                                                    confidence: Double(top.confidence))
                        }
                    } else if !self.unsupportedOutputReported {
                        self.unsupportedOutputReported = true
                        if self.activeProfile == .night {
                            self.lowLightDetector = nil
                            self.lowLightError = "Low-light model returned unsupported detection outputs"
                        }
                        self.report("YOLO loaded but returns raw tensors, not detection boxes. Export YOLO with NMS or add a tensor decoder.")
                    }
                }
                let finished = ProcessInfo.processInfo.systemUptime
                if finished - self.lastInferenceReport >= 2, self.detector != nil {
                    self.lastInferenceReport = finished
                    self.report(String(format: "N5 · %@ YOLO11n %.0f ms · frame age %.0f ms · %d vehicles",
                        self.activeProfile.title,
                        (finished - started) * 1000, (finished - uptime) * 1000, vehicles.count))
                }
                guard self.isCurrentFrame(submittedGeneration) else { return }
                let lane = self.laneTracker.update(buffer, excluding: vehicles.map { $0.box }, at: uptime)
                self.onLane?(lane)
                let road = self.laneTracker.roadRegion
                self.onRoad?(road)
                // A manually selected vehicle takes priority over automatic selection.
                if let request = self.manualRequest, let id = self.targetID {
                    self.onDetections?(vehicles, uptime)
                    try self.sequence.perform([request], on: buffer, orientation: .up)
                    guard
                        let observation = request.results?.first as? VNDetectedObjectObservation,
                        observation.confidence >= 0.65,
                        self.usable(observation.boundingBox),
                        let distance = DriveMath.calibratedDistance(
                            referenceMeters: self.referenceDistance,
                            referenceWidth: self.referenceWidth,
                            currentWidth: Double(observation.boundingBox.width)
                        )
                    else {
                        self.lose(id, "Calibrated target lost.")
                        return
                    }
                    let box = observation.boundingBox
                    if let previous = self.previousBox {
                        let ratio = box.width / previous.width
                        guard (0.75...1.33).contains(ratio) else {
                            self.lose(id, "Target size jumped; calibration cleared.")
                            return
                        }
                    }
                    request.inputObservation = observation
                    self.previousBox = box
                    guard self.isCurrentFrame(submittedGeneration) else { return }
                    self.onReading?(TrackingReading(targetID: id, box: box,
                                                   distance: distance,
                                                   confidence: Double(observation.confidence),
                                                   uptime: uptime,
                                                   rangeSource: "CALIBRATED RANGE · entered measured gap",
                                                   detectorGeneration: self.detectorGeneration))
                    return
                }
                var trackedBox: CGRect?
                if let request = self.automaticRequest {
                    // Failure of this auxiliary tracker must not discard fresh YOLO boxes.
                    do {
                        try self.automaticSequence.perform([request], on: buffer, orientation: .up)
                        if let observation = request.results?.first as? VNDetectedObjectObservation,
                           observation.confidence >= 0.60, self.usable(observation.boundingBox) {
                            trackedBox = observation.boundingBox
                            request.inputObservation = observation
                        } else { self.automaticRequest = nil }
                    } catch { self.automaticRequest = nil }
                }
                let classified = self.headingTracker.update(vehicles, buffer: buffer, lane: lane,
                    at: uptime, preferredBox: trackedBox ?? self.previousBox,
                    preferredID: self.targetID, trackedBox: trackedBox)
                self.onDetections?(classified.map {
                    VehicleDetection(id: $0.trackID ?? UUID(), box: $0.box,
                        label: $0.label, confidence: $0.confidence,
                        viewpoint: $0.viewpoint, capturedAt: uptime)
                }, uptime)
                let ego = self.egoSamples.last { $0.measurementUptime <= uptime && uptime - $0.measurementUptime <= 1.5 }
                let candidates = self.motionGuard.update(classified, at: uptime, horizontalFOV: self.horizontalFOV, ego: ego)
                guard self.isCurrentFrame(submittedGeneration) else { return }
                let imageAspect = Double(CVPixelBufferGetWidth(buffer)) / Double(CVPixelBufferGetHeight(buffer))
                self.selectAutomatic(candidates, at: uptime, lane: lane, road: road,
                                     trackedBox: trackedBox, imageAspect: imageAspect)
            } catch {
                if let id = self.targetID { self.lose(id, "Vision error: \(error.localizedDescription)") }
                self.report("Vision error: \(error.localizedDescription)")
            }
        }
    }

    private func updateDetectorLighting(_ buffer: CVPixelBuffer, at time: TimeInterval,
                                        iso: Double?, exposure: Double?) {
        if lighting.shouldSample(at: time), let sample = lightingSample(buffer, iso: iso, exposure: exposure) {
            lighting.update(sample, at: time)
        }
        let requested = lighting.profile
        let available = requested == .night && lowLightDetector != nil
            ? DetectorLightingProfile.night : .day
        if available != activeProfile {
            detector = available == .night ? lowLightDetector : standardDetector
            activeProfile = available; detectorGeneration += 1
            detectorSettlingUntil = time + 0.6
            unsupportedOutputReported = false
            // Keep object IDs, rear confirmation, lane evidence and Vision
            // association. Discard only derivatives across different box models.
            motionGuard.reset()
            publishLighting(at: time, force: true)
        } else { publishLighting(at: time) }
    }

    private func publishLighting(at time: TimeInterval, force: Bool = false) {
        guard force || time - lastLightingReport >= 2 else { return }
        lastLightingReport = time
        let luma = lighting.smoothedLuma.map { String(format: " · scene %.0f%%", $0 * 100) } ?? ""
        let fallback = lighting.profile == .night && lowLightDetector == nil
            ? " · night unavailable; standard fallback" : ""
        let error = lowLightError.map { " · \($0)" } ?? ""
        onLightingStatus?("\(lighting.mode.title) · \(activeProfile.title) YOLO11n\(luma)\(fallback)\(error)")
    }

    private func lightingSample(_ buffer: CVPixelBuffer, iso: Double?, exposure: Double?) -> DetectorLightingSample? {
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let format = CVPixelBufferGetPixelFormatType(buffer)
        let planar = CVPixelBufferGetPlaneCount(buffer) == 2
        guard !planar || format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
            || format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange else { return nil }
        let width = planar ? CVPixelBufferGetWidthOfPlane(buffer, 0) : CVPixelBufferGetWidth(buffer)
        let height = planar ? CVPixelBufferGetHeightOfPlane(buffer, 0) : CVPixelBufferGetHeight(buffer)
        let rowBytes = planar ? CVPixelBufferGetBytesPerRowOfPlane(buffer, 0) : CVPixelBufferGetBytesPerRow(buffer)
        guard width > 0, height > 0,
              let base = planar ? CVPixelBufferGetBaseAddressOfPlane(buffer, 0) : CVPixelBufferGetBaseAddress(buffer),
              planar || format == kCVPixelFormatType_32BGRA || format == kCVPixelFormatType_32RGBA else { return nil }
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        var samples: [Double] = []
        samples.reserveCapacity(768)
        for y in Swift.stride(from: height / 8, to: height * 7 / 8, by: max(1, height / 24)) {
            for x in Swift.stride(from: width / 10, to: width * 9 / 10, by: max(1, width / 32)) {
                let luma: Double
                if planar {
                    let value = Double(bytes[y * rowBytes + x])
                    luma = format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
                        ? max(0, min(1, (value - 16) / 219)) : value / 255
                } else {
                    let index = y * rowBytes + x * 4
                    let red = Double(bytes[index + (format == kCVPixelFormatType_32BGRA ? 2 : 0)])
                    let green = Double(bytes[index + 1])
                    let blue = Double(bytes[index + (format == kCVPixelFormatType_32BGRA ? 0 : 2)])
                    luma = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
                }
                samples.append(luma)
            }
        }
        guard !samples.isEmpty else { return nil }
        let darkFraction = Double(samples.filter { $0 < 0.20 }.count) / Double(samples.count)
        samples.sort()
        return DetectorLightingSample(medianLuma: samples[samples.count / 2], darkFraction: darkFraction,
            iso: iso, exposureSeconds: exposure)
    }
    private func selectAutomatic(_ detections: [LeadVehicle], at time: TimeInterval,
                                 lane: LanePath?, road: RoadRegion?, trackedBox: CGRect?,
                                 imageAspect: Double) {
        guard let fov = horizontalFOV else {
            automaticSelector.reset()
            hazardSelector.reset()
            onLeadStatus?("Waiting for camera geometry")
            clearAutomaticReading("Waiting for camera geometry.")
            return
        }
        let candidates = detections.filter { estimatedDistance(for: $0, fov: fov) != nil }
        let rejected = candidates.filter { $0.travel?.vetoesLead(at: time) == true }.count
        // A clipped range cannot erase the confirmed object identity. It can
        // still supply a fresh box; its numbers remain unavailable until whole.
        let lead = automaticSelector.update(detections, at: time, lane: lane,
            trackedBox: trackedBox, road: road, imageAspect: imageAspect)
        let hazard = hazardSelector.update(candidates, at: time, lane: lane, road: road, imageAspect: imageAspect)
        let chosen: LeadSelection?
        let leadClosingTime = lead.flatMap { selection -> Double? in
            guard selection.isFreshDetection, selection.isPathSupported,
                  let closing = selection.vehicle.travel?.closingSpeed, closing > 0.5,
                  let distance = estimatedDistance(for: selection.vehicle, fov: fov) else { return nil }
            return distance / closing
        } ?? .infinity
        if let hazard, let hazardRange = estimatedDistance(for: hazard.vehicle, fov: fov),
           lead?.isPathSupported != true || lead?.isFreshDetection != true
            || hazard.id == lead?.id
            || hazardRange / max(0.001, hazard.vehicle.travel?.closingSpeed ?? 0) < leadClosingTime * 0.90 {
            chosen = hazard
        } else { chosen = lead ?? hazard }
        guard let selection = chosen else {
            let reason: String
            if detections.isEmpty { reason = "No vehicle detected" }
            else if candidates.isEmpty { reason = "Vehicle clipped · range unavailable" }
            else if rejected == candidates.count { reason = "Closing vehicle · checking path" }
            else if candidates.allSatisfy({ $0.viewpoint?.isFrontFacing(at: time) == true }) {
                reason = "Front view · checking hazard path"
            } else if candidates.contains(where: { $0.viewpoint?.hasConfirmedRearView(at: time) == true }) {
                reason = "Rear view · confirming track"
            } else if candidates.contains(where: { $0.viewpoint?.isRearFacing(at: time) == true }) {
                reason = "Rear view · checking again"
            } else if candidates.allSatisfy({ !["car", "truck"].contains($0.label) }) {
                reason = "Vehicle view unsupported"
            } else { reason = "Vehicle detected · checking view" }
            onLeadStatus?(reason)
            clearAutomaticReading(reason)
            return
        }
        let distance = estimatedDistance(for: selection.vehicle, fov: fov)
        let hasRearIdentity = selection.hasConfirmedRearIdentity
            || (selection.id == lead?.id && lead?.hasConfirmedRearIdentity == true)
            || selection.vehicle.viewpoint?.hasConfirmedRearView(at: time) == true
        let rearHeld = hasRearIdentity && (!selection.isFreshDetection
            || selection.isRearConfirmationRetained
            || (selection.id == lead?.id && lead?.isRearConfirmationRetained == true)
            || selection.vehicle.viewpoint?.hasConfirmedRearView(at: time) != true)
        if let oldID = targetID, oldID != selection.id {
            onLost?(oldID, "Lead candidate changed; collecting fresh measurements.")
        }
        let changed = targetID != selection.id
        targetID = selection.id
        previousBox = selection.vehicle.box
        if selection.isFreshDetection {
            // Reseed from each detector measurement to prevent visual tracker drift.
            let request = VNTrackObjectRequest(detectedObjectObservation:
                VNDetectedObjectObservation(boundingBox: selection.vehicle.box))
            request.trackingLevel = .accurate
            automaticRequest = request
            if changed { automaticSequence = VNSequenceRequestHandler() }
        }
        let rangeSource: String
        if distance == nil { rangeSource = "Lead held · vehicle clipped · gap measurements paused" }
        else if !selection.isFreshDetection { rangeSource = "Brief detection gap · visual tracking only" }
        else if !selection.isPathSupported { rangeSource = "Candidate · driving path/direction uncertain · lead speed/risk paused" }
        else if selection.isCameraProjectedPath { rangeSource = "APPROX. RANGE · camera-projected path · lane position uncertain" }
        else if selection.isCollisionHazard { rangeSource = "APPROX. RANGE · closing vehicle in measured/projected driving path" }
        else if lane?.isUsable(at: time) != true || lane?.bounds(at: Double(selection.vehicle.box.minY)) == nil {
            rangeSource = "APPROX. RANGE · road area + forward motion"
        }
        else { rangeSource = String(format: "APPROX. RANGE · assumed %.1f m vehicle width", selection.vehicle.assumedWidthMeters) }
        reportLead(rangeSource)
        let shortID = String(selection.id.uuidString.prefix(4))
        onLeadStatus?(distance == nil ? "Lead \(shortID) held · vehicle clipped"
            : !selection.isFreshDetection ? "Lead \(shortID) held · detection gap"
            : !selection.isPathSupported ? "Lead \(shortID) · path uncertain"
            : selection.isCameraProjectedPath ? "Possible obstacle · gap closing"
            : selection.isCollisionHazard ? "Closing vehicle ahead"
            : rearHeld ? "Lead \(shortID) · rear confirmation held" : "Lead \(shortID) confirmed")
        onReading?(TrackingReading(targetID: selection.id, box: selection.vehicle.box,
            distance: distance, confidence: selection.vehicle.confidence, uptime: time,
            rangeSource: rangeSource, isFreshDetection: selection.isFreshDetection,
            isPathSupported: selection.isPathSupported, isCollisionHazard: selection.isCollisionHazard,
            isCameraProjectedPath: selection.isCameraProjectedPath,
            hasConfirmedRearIdentity: hasRearIdentity, isRearConfirmationRetained: rearHeld,
            approachClosingSpeed: selection.isCollisionHazard ? selection.vehicle.travel?.closingSpeed : nil,
            canEstimateLeadSpeed: hasRearIdentity
                && selection.vehicle.travel?.vetoesLead(at: time) != true,
            detectorGeneration: detectorGeneration,
            isDetectorSettling: time < detectorSettlingUntil))
    }

    private func estimatedDistance(for vehicle: LeadVehicle, fov: Double) -> Double? {
        guard LeadVehicle.validBox(vehicle.box),
              vehicle.box.minX > 0.005, vehicle.box.maxX < 0.995,
              vehicle.box.minY > 0.005, vehicle.box.maxY < 0.995 else { return nil }
        let distance = vehicle.assumedWidthMeters /
            (2 * tan(fov * .pi / 360) * Double(vehicle.box.width))
        return distance.isFinite && (1...150).contains(distance) ? distance : nil
    }

    private func clearAutomaticReading(_ reason: String) {
        reportLead(reason)
        let oldID = targetID
        targetID = nil
        previousBox = nil
        automaticRequest = nil
        automaticSequence = VNSequenceRequestHandler()
        if let oldID { onLost?(oldID, reason) }
    }
    private func reportLead(_ status: String) {
        leadStatus = status
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastLeadReport >= 0.75 else { return }
        lastLeadReport = now
        onStatus?("\(vehicleStatus) · \(laneStatus) · \(headingStatus) · \(status)")
    }
    private func usable(_ box: CGRect) -> Bool {
        box.minX >= 0 && box.maxX <= 1 && box.minY >= 0 && box.maxY <= 1 &&
            box.width > 0.015 && box.height > 0.015
    }
    private func lose(_ id: UUID, _ message: String) {
        resetTarget()
        onLost?(id, message)
    }
    private func report(_ message: String) {
        vehicleStatus = message
        onStatus?("\(message) · \(laneStatus) · \(headingStatus) · \(leadStatus)")
        print("[BrakeCheck YOLO] \(message)")
    }
    enum TrackerError: LocalizedError {
        case modelMissing, nightModelMissing, modelContract
        var errorDescription: String? {
            switch self {
            case .modelMissing: return "YOLOModelData resource not found in app bundle."
            case .nightModelMissing: return "Night detector resource not found in app bundle."
            case .modelContract: return "Detector must provide a 640-pixel image input and NMS confidence/coordinate outputs."
            }
        }
    }
}
