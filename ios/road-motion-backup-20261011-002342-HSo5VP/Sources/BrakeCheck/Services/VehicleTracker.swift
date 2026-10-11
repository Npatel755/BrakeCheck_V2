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
}
struct TrackingReading {
    let targetID: UUID
    let box: CGRect
    let distance: Double
    let confidence: Double
    let uptime: TimeInterval
    let rangeSource: String
    var isFreshDetection = true
    var isPathSupported = true
}
/// Keeps Vision state on one serial queue and drops frames if inference is busy.
final class VehicleTracker {
    private let queue = DispatchQueue(label: "BrakeCheck.vision", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false
    private var detector: VNCoreMLRequest?
    private var prepared = false
    private var unsupportedOutputReported = false
    private var vehicleStatus = "YOLO waiting"
    private var laneStatus = "YOLOP lanes waiting"
    private var headingStatus = "Vehicle front/rear model waiting"
    private var manualRequest: VNTrackObjectRequest?
    private var sequence = VNSequenceRequestHandler()
    private var targetID: UUID?
    private var previousBox: CGRect?
    private var automaticSelector = LeadSelector(requiresRearView: true)
    private let laneTracker = LaneTracker()
    private let headingTracker = VehicleHeadingTracker()
    private var automaticRequest: VNTrackObjectRequest?
    private var automaticSequence = VNSequenceRequestHandler()
    var onLane: ((LanePath?) -> Void)?
    private var referenceDistance = 0.0
    private var referenceWidth = 0.0
    private var horizontalFOV: Double?
    /// Existing callers can continue to assign these callbacks.
    var onReading: ((TrackingReading) -> Void)?
    var onLost: ((UUID, String) -> Void)?
    var onDetections: (([VehicleDetection]) -> Void)?
    var onStatus: ((String) -> Void)?
    init() {
        laneTracker.onStatus = { [weak self] status in
            guard let self else { return }
            self.queue.async {
                self.laneStatus = status
                self.onStatus?("\(self.vehicleStatus) · \(status) · \(self.headingStatus)")
                print("[BrakeCheck lanes] \(status)")
            }
        }
        headingTracker.onStatus = { [weak self] status in
            guard let self else { return }
            self.queue.async {
                self.headingStatus = status
                self.onStatus?("\(self.vehicleStatus) · \(self.laneStatus) · \(status)")
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
        laneTracker.reset()
        headingTracker.reset()
        automaticRequest = nil
        automaticSequence = VNSequenceRequestHandler()
        onLane?(nil)
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
        lock.unlock()
        queue.async {
            defer {
                self.lock.lock()
                self.busy = false
                self.lock.unlock()
            }
            do {
                var vehicles: [VehicleDetection] = []
                if let detector = self.detector {
                    // Change .up if the camera output buffer is physically rotated.
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
                self.onDetections?(vehicles)
                let lane = self.laneTracker.update(buffer, excluding: vehicles.map { $0.box }, at: uptime)
                self.onLane?(lane)
                // A manually selected vehicle takes priority over automatic selection.
                if let request = self.manualRequest, let id = self.targetID {
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
                           observation.confidence >= 0.45, self.usable(observation.boundingBox) {
                            trackedBox = observation.boundingBox
                            request.inputObservation = observation
                        } else { self.automaticRequest = nil }
                    } catch { self.automaticRequest = nil }
                }
                let candidates = self.headingTracker.update(vehicles, buffer: buffer, lane: lane,
                    at: uptime, preferredBox: self.previousBox)
                self.selectAutomatic(candidates, at: uptime, lane: lane, trackedBox: trackedBox)
            } catch {
                if let id = self.targetID { self.lose(id, "Vision error: \(error.localizedDescription)") }
                self.report("Vision error: \(error.localizedDescription)")
            }
        }
    }
    private func selectAutomatic(_ detections: [LeadVehicle], at time: TimeInterval,
                                 lane: LanePath?, trackedBox: CGRect?) {
        guard let fov = horizontalFOV else {
            automaticSelector.reset()
            clearAutomaticReading("Waiting for camera geometry.")
            return
        }
        let candidates = detections.filter { estimatedDistance(for: $0, fov: fov) != nil }
        guard let selection = automaticSelector.update(candidates, at: time, lane: lane, trackedBox: trackedBox),
              let distance = estimatedDistance(for: selection.vehicle, fov: fov) else {
            clearAutomaticReading("Checking vehicle tracks · need two independent rear views.")
            return
        }
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
        if !selection.isFreshDetection { rangeSource = "Brief detection gap · visual tracking only" }
        else if !selection.isPathSupported { rangeSource = "Rear-view candidate · lane uncertain · estimates paused" }
        else { rangeSource = String(format: "APPROX. RANGE · assumed %.1f m vehicle width", selection.vehicle.assumedWidthMeters) }
        onReading?(TrackingReading(targetID: selection.id, box: selection.vehicle.box,
            distance: distance, confidence: selection.vehicle.confidence, uptime: time,
            rangeSource: rangeSource, isFreshDetection: selection.isFreshDetection,
            isPathSupported: selection.isPathSupported))
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
        let oldID = targetID
        targetID = nil
        previousBox = nil
        automaticRequest = nil
        automaticSequence = VNSequenceRequestHandler()
        if let oldID { onLost?(oldID, reason) }
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
        onStatus?("\(message) · \(laneStatus) · \(headingStatus)")
        print("[BrakeCheck YOLO] \(message)")
    }
    enum TrackerError: LocalizedError {
        case modelMissing
        var errorDescription: String? {
            "YOLOModelData resource not found in app bundle."
        }
    }
}
