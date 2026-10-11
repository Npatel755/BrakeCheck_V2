import Foundation
import CoreGraphics
import Vision
import CoreML
import CoreVideo
import ImageIO
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
}
/// Keeps Vision state on one serial queue and drops frames if inference is busy.
final class VehicleTracker {
    private let queue = DispatchQueue(label: "BrakeCheck.vision", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false
    private var frameGeneration = 0
    private var detector: VNCoreMLRequest?
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
                let url = try self.locateModel()
                let compiledURL: URL
                if url.pathExtension.lowercased() == "mlmodelc" {
                    compiledURL = url
                } else {
                    compiledURL = try MLModel.compileModel(at: url)
                }
                let options = MLModelConfiguration()
                options.computeUnits = .all
                let mlModel = try MLModel(contentsOf: compiledURL, configuration: options)
                let visionModel = try VNCoreMLModel(for: mlModel)
                let request = VNCoreMLRequest(model: visionModel)
                request.imageCropAndScaleOption = .scaleFit
                self.report("YOLO warming up…")
                // Discard a cold-start prediction before processing live
                // camera frames; it never supplies a box or rear evidence.
                var warmBuffer: CVPixelBuffer?
                if CVPixelBufferCreate(kCFAllocatorDefault, 640, 640,
                    kCVPixelFormatType_32BGRA, nil, &warmBuffer) == kCVReturnSuccess,
                   let warmBuffer {
                    if CVPixelBufferLockBaseAddress(warmBuffer, []) == kCVReturnSuccess {
                        if let base = CVPixelBufferGetBaseAddress(warmBuffer) {
                            memset(base, 127, CVPixelBufferGetBytesPerRow(warmBuffer) * CVPixelBufferGetHeight(warmBuffer))
                        }
                        CVPixelBufferUnlockBaseAddress(warmBuffer, [])
                    }
                    try VNImageRequestHandler(cvPixelBuffer: warmBuffer, orientation: .up).perform([request])
                }
                self.detector = request
                self.report("YOLO loaded. Searching for vehicles...")
            } catch {
                self.prepared = false // allow a later retry
                self.detector = nil
                self.report("YOLO model could not load: \(error.localizedDescription)")
            }
        }
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
                return try copyPackageForRuntime(from: source)
            }
            // Some SwiftPM/xTool bundles expose copied resources via resourceURL.
            if let source = bundle.resourceURL?.appendingPathComponent("YOLOModelData"),
               FileManager.default.fileExists(atPath: source.appendingPathComponent("Manifest.json").path) {
                return try copyPackageForRuntime(from: source)
            }
        }
        throw TrackerError.modelMissing
    }
    private func copyPackageForRuntime(from source: URL) throws -> URL {
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("BrakeCheckYOLO-\(UUID().uuidString).mlpackage", isDirectory: true)
        try FileManager.default.copyItem(at: source, to: destination)
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
    func submit(_ buffer: CVPixelBuffer, uptime: TimeInterval) {
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
                        self.report("YOLO loaded but returns raw tensors, not detection boxes. Export YOLO with NMS or add a tensor decoder.")
                    }
                }
                let finished = ProcessInfo.processInfo.systemUptime
                if finished - self.lastInferenceReport >= 2, self.detector != nil {
                    self.lastInferenceReport = finished
                    self.report(String(format: "N4 · YOLO %.0f ms · frame age %.0f ms · %d vehicles",
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
                                                   rangeSource: "CALIBRATED RANGE · entered measured gap"))
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
                && selection.vehicle.travel?.vetoesLead(at: time) != true))
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
        case modelMissing
        var errorDescription: String? {
            "YOLOModelData resource not found in app bundle."
        }
    }
}
