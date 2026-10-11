import BrakeCheckCore
import CoreGraphics
import CoreImage
import CoreML
import CoreVideo
import CryptoKit
import Foundation

/// A trained front/rear/side check, not a headlight or road-side heuristic.
/// Classification runs separately from YOLO, retaining at most one frame.
/// Cache association stays on VehicleTracker's queue. Unmatched/expired results
/// never authorize a new target, and old-trip results cannot enter a new trip.
final class VehicleHeadingTracker {
    var onStatus: ((String) -> Void)?
    private let loadingQueue = DispatchQueue(label: "BrakeCheck.viewpoint-load", qos: .utility)
    private let predictionQueue = DispatchQueue(label: "BrakeCheck.viewpoint", qos: .userInitiated,
                                                autoreleaseFrequency: .workItem)
    private let lock = NSLock()
    private var loading = false
    private var model: MLModel?
    private var busy = false
    private var generation = 0
    private var completed: Evidence?
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var pool: CVPixelBufferPool?
    private var lastPrediction: TimeInterval = -.infinity
    private var lastStatus: TimeInterval = -.infinity
    private var tracks = VehicleHeadingTracks()
    private var scheduler = VehicleViewpointScheduler()
    private var lastInferenceMs = 0
    private struct Evidence {
        let trackID: UUID
        let viewpoint: VehicleViewpoint
        let inferenceMs: Int
    }

    func prepare() {
        lock.lock()
        guard !loading, model == nil else { lock.unlock(); return }
        loading = true; lock.unlock()
        loadingQueue.async {
            self.onStatus?("Vehicle front/rear model loading…")
            do {
                let source = try self.locateModel()
                let compiled = try self.compiledModel(from: source)
                let options = MLModelConfiguration(); options.computeUnits = .all
                let loaded: MLModel
                do { loaded = try MLModel(contentsOf: compiled, configuration: options) }
                catch {
                    try FileManager.default.removeItem(at: compiled)
                    loaded = try MLModel(contentsOf: self.compiledModel(from: source), configuration: options)
                }
                guard let input = loaded.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
                      input.pixelsWide == 224, input.pixelsHigh == 224,
                      loaded.modelDescription.outputDescriptionsByName["viewpoint_scores"] != nil else {
                    throw ViewpointError.contract
                }
                self.onStatus?("Vehicle front/rear model warming up…")
                // Prime Core ML's first prediction before accepting live crops.
                // The uniform image is discarded and never counts as evidence.
                try self.predictionQueue.sync { try self.warmUp(loaded) }
                self.lock.lock(); self.model = loaded; self.loading = false; self.lock.unlock()
                self.onStatus?("Vehicle front/rear model ready")
            } catch {
                self.lock.lock(); self.loading = false; self.lock.unlock()
                self.onStatus?("Front/rear model unavailable: \(error.localizedDescription)")
            }
        }
    }

    func reset() {
        tracks.reset(); scheduler.reset(); lastPrediction = -.infinity; lastStatus = -.infinity; lastInferenceMs = 0
        lock.lock(); generation += 1; completed = nil; lock.unlock()
    }

    func update(_ vehicles: [VehicleDetection], buffer: CVPixelBuffer, lane: LanePath?,
                at time: TimeInterval, preferredID: UUID? = nil,
                trackedBox: CGRect? = nil, allowNightClassAliases: Bool = false) -> [LeadVehicle] {
        guard time.isFinite else { reset(); return [] }
        let linked = tracks.update(vehicles.map {
            LeadVehicle(box: $0.box, label: $0.label, confidence: $0.confidence)
        }, at: time, preferredID: preferredID, trackedBox: trackedBox,
            allowNightClassAliases: allowNightClassAliases)
        lock.lock()
        let finished = completed; completed = nil
        let classifier = model, canPredict = !busy
        lock.unlock()
        if let finished {
            lastInferenceMs = finished.inferenceMs
            tracks.record(finished.viewpoint, for: finished.trackID, at: time)
        }
        let result = linked.map { vehicle in
            LeadVehicle(box: vehicle.box, label: vehicle.label, confidence: vehicle.confidence,
                viewpoint: vehicle.trackID.flatMap { tracks.viewpoint(for: $0, at: time) }, trackID: vehicle.trackID)
        }
        if let classifier, canPredict, time - lastPrediction >= 0.25 {
            let width = Double(CVPixelBufferGetWidth(buffer)), height = Double(CVPixelBufferGetHeight(buffer))
            let eligible = result.filter { vehicle in
                // YOLO labels some SUVs/pickups as truck. Run their own crop
                // through the viewpoint model instead of silently skipping
                // them. CFV is car-trained: truck views remain best-effort,
                // require independent rear evidence, and still need measured
                // lane or road-plus-motion support before authorizing metrics.
                guard ["car", "truck"].contains(vehicle.label), vehicle.confidence >= 0.40,
                      LeadVehicle.validBox(vehicle.box),
                      Double(vehicle.box.width) * width >= 32, Double(vehicle.box.height) * height >= 32,
                      Double(vehicle.box.minY) < 0.75 else { return false }
                return true
            }
            if let vehicle = scheduler.choose(eligible, at: time, preferredID: preferredID, lane: lane),
               let id = vehicle.trackID {
                lastPrediction = time
                schedule(classifier, vehicle: vehicle, trackID: id, buffer: buffer, at: time)
            }
        }
        if classifier != nil, time - lastStatus >= 2 {
            lastStatus = time
            let front = result.filter { $0.viewpoint?.isFrontFacing(at: time) == true }.count
            let rear = result.filter { $0.viewpoint?.hasConfirmedRearView(at: time) == true }.count
            let pending = result.filter { $0.viewpoint?.isRearFacing(at: time) == true
                && $0.viewpoint?.hasConfirmedRearView(at: time) != true }.count
            onStatus?("Front/rear · \(lastInferenceMs) ms · \(front) facing camera · \(rear) confirmed rear · \(pending) checking again · \(result.count - front - rear - pending) uncertain")
        }
        return result
    }

    private func schedule(_ classifier: MLModel, vehicle: LeadVehicle, trackID: UUID,
                          buffer: CVPixelBuffer, at time: TimeInterval) {
        lock.lock()
        guard !busy else { lock.unlock(); return }
        busy = true; let submittedGeneration = generation; lock.unlock()
        predictionQueue.async {
            defer { self.lock.lock(); self.busy = false; self.lock.unlock() }
            let started = ProcessInfo.processInfo.systemUptime
            self.lock.lock(); let currentGeneration = self.generation; self.lock.unlock()
            guard currentGeneration == submittedGeneration, started - time <= 0.40 else { return }
            do {
                let input = try self.makeInput(buffer, vehicle: vehicle.box)
                let provider = try MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: input)])
                let output = try classifier.prediction(from: provider)
                guard let scores = output.featureValue(for: "viewpoint_scores")?.multiArrayValue,
                      scores.count == 8,
                      let viewpoint = VehicleViewpoint(scores: (0..<8).map { scores[$0].doubleValue }, capturedAt: time) else {
                    throw ViewpointError.output
                }
                let finished = ProcessInfo.processInfo.systemUptime
                self.lock.lock()
                if self.generation == submittedGeneration, viewpoint.isFresh(at: finished) {
                    self.completed = Evidence(trackID: trackID, viewpoint: viewpoint,
                                              inferenceMs: Int((finished - started) * 1000))
                }
                self.lock.unlock()
                if !viewpoint.isFresh(at: finished) {
                    self.onStatus?("Front/rear check · \(Int((finished - started) * 1000)) ms · result too old")
                }
            } catch {
                self.onStatus?("Front/rear check failed: \(error.localizedDescription)")
            }
        }
    }

    private func inputBuffer() throws -> CVPixelBuffer {
        if pool == nil {
            let attributes: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: 224, kCVPixelBufferHeightKey as String: 224,
                kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
            guard CVPixelBufferPoolCreate(kCFAllocatorDefault, nil, attributes as CFDictionary, &pool) == kCVReturnSuccess
            else { throw ViewpointError.frame }
        }
        guard let pool else { throw ViewpointError.frame }
        var output: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &output) == kCVReturnSuccess,
              let output else { throw ViewpointError.frame }
        return output
    }

    private func warmUp(_ classifier: MLModel) throws {
        let input = try inputBuffer()
        let rect = CGRect(x: 0, y: 0, width: 224, height: 224)
        let gray = CIImage(color: CIColor(red: 114.0 / 255, green: 114.0 / 255, blue: 114.0 / 255))
            .cropped(to: rect)
        context.render(gray, to: input, bounds: rect, colorSpace: CGColorSpaceCreateDeviceRGB())
        let provider = try MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: input)])
        _ = try classifier.prediction(from: provider)
    }

    private func makeInput(_ buffer: CVPixelBuffer, vehicle: CGRect) throws -> CVPixelBuffer {
        let output = try inputBuffer()
        let image = CIImage(cvPixelBuffer: buffer)
        let box = CGRect(x: image.extent.minX + vehicle.minX * image.extent.width,
                         y: image.extent.minY + vehicle.minY * image.extent.height,
                         width: vehicle.width * image.extent.width, height: vehicle.height * image.extent.height)
        // Isolate this vehicle: adjacent front/rear views must not contaminate
        // its label. Tight crops were also checked on labeled CFV photographs.
        let width = box.width * 1.06, height = box.height * 1.06
        let region = CGRect(x: box.midX - width / 2, y: box.midY - height / 2, width: width, height: height)
        let rect = CGRect(x: 0, y: 0, width: 224, height: 224)
        let crop = image.cropped(to: region)
            .transformed(by: CGAffineTransform(translationX: -region.minX, y: -region.minY))
            .transformed(by: CGAffineTransform(scaleX: 224 / width, y: 224 / height))
        let background = CIImage(color: CIColor(red: 114.0 / 255, green: 114.0 / 255, blue: 114.0 / 255)).cropped(to: rect)
        context.render(crop.composited(over: background).cropped(to: rect), to: output,
                       bounds: rect, colorSpace: CGColorSpaceCreateDeviceRGB())
        return output
    }

    private func locateModel() throws -> URL {
        for bundle in [Bundle.module, Bundle.main] + Bundle.allBundles + Bundle.allFrameworks {
            if let directory = bundle.url(forResource: "ViewpointModelData", withExtension: nil) {
                let file = directory.appendingPathComponent("VehicleViewpoint.mlmodel")
                if FileManager.default.fileExists(atPath: file.path) { return file }
            }
            if let file = bundle.resourceURL?.appendingPathComponent("ViewpointModelData/VehicleViewpoint.mlmodel"),
               FileManager.default.fileExists(atPath: file.path) { return file }
            if let file = bundle.url(forResource: "VehicleViewpoint", withExtension: "mlmodel") { return file }
        }
        throw ViewpointError.missing
    }
    private func compiledModel(from source: URL) throws -> URL {
        let digest = SHA256.hash(data: try Data(contentsOf: source)).map { String(format: "%02x", $0) }.joined()
        let directory = try FileManager.default.url(for: .cachesDirectory, in: .userDomainMask,
                                                   appropriateFor: nil, create: true)
            .appendingPathComponent("BrakeCheckViewpointModels", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent("CFV-\(digest.prefix(16)).mlmodelc", isDirectory: true)
        if FileManager.default.fileExists(atPath: destination.path) { return destination }
        let compiled = try MLModel.compileModel(at: source)
        defer { try? FileManager.default.removeItem(at: compiled) }
        let staged = directory.appendingPathComponent("stage-\(UUID().uuidString).mlmodelc", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: staged) }
        try FileManager.default.copyItem(at: compiled, to: staged)
        try FileManager.default.moveItem(at: staged, to: destination)
        return destination
    }
}

private enum ViewpointError: LocalizedError {
    case missing, contract, frame, output
    var errorDescription: String? {
        switch self {
        case .missing: return "ViewpointModelData/VehicleViewpoint.mlmodel is missing"
        case .contract: return "expected CFV RGB 224×224 input and viewpoint_scores output"
        case .frame: return "vehicle image could not be resized"
        case .output: return "expected eight finite CFV viewpoint probabilities"
        }
    }
}
