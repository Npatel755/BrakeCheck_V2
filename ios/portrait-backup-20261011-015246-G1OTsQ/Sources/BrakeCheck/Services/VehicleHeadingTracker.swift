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
    private var tracks: [Evidence] = []
    private struct Evidence {
        var box: CGRect
        let label: String
        var viewpoint: VehicleViewpoint
        var seenAt: TimeInterval
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
                self.lock.lock(); self.model = loaded; self.loading = false; self.lock.unlock()
                self.onStatus?("Vehicle front/rear model ready")
            } catch {
                self.lock.lock(); self.loading = false; self.lock.unlock()
                self.onStatus?("Front/rear model unavailable: \(error.localizedDescription)")
            }
        }
    }

    func reset() {
        tracks = []; lastPrediction = -.infinity; lastStatus = -.infinity
        lock.lock(); generation += 1; completed = nil; lock.unlock()
    }

    func update(_ vehicles: [VehicleDetection], buffer: CVPixelBuffer, lane: LanePath?,
                at time: TimeInterval, preferredBox: CGRect?) -> [LeadVehicle] {
        guard time.isFinite else { reset(); return [] }
        tracks.removeAll { time < $0.seenAt || time - $0.seenAt > 0.90 }
        lock.lock()
        let finished = completed; completed = nil
        let classifier = model, canPredict = !busy
        lock.unlock()
        if let finished, finished.viewpoint.isFresh(at: time) {
            let candidates = vehicles.filter { $0.label == finished.label && Self.matches($0.box, finished.box) }
            if candidates.count == 1, let vehicle = candidates.first {
                let existing = tracks.indices.filter { tracks[$0].label == vehicle.label
                    && Self.matches(tracks[$0].box, vehicle.box) }
                if existing.count == 1, let index = existing.first {
                    tracks[index] = Evidence(box: vehicle.box, label: vehicle.label,
                                             viewpoint: finished.viewpoint, seenAt: time)
                } else if existing.isEmpty {
                    tracks.append(Evidence(box: vehicle.box, label: vehicle.label,
                                            viewpoint: finished.viewpoint, seenAt: time))
                }
            }
        }
        var used: Set<Int> = []
        var linked: [Int: Int] = [:]
        for (index, vehicle) in vehicles.enumerated() {
            let matches = tracks.indices.filter { !used.contains($0) && tracks[$0].label == vehicle.label
                && Self.matches(vehicle.box, tracks[$0].box) }.sorted {
                    Self.overlap(vehicle.box, tracks[$0].box) > Self.overlap(vehicle.box, tracks[$1].box)
                }
            guard let best = matches.first else { continue }
            // Crossing/overlapping detections must not borrow a rear label.
            if matches.count > 1,
               Self.overlap(vehicle.box, tracks[best].box) - Self.overlap(vehicle.box, tracks[matches[1]].box) < 0.12 {
                continue
            }
            used.insert(best); linked[index] = best
            tracks[best].box = vehicle.box; tracks[best].seenAt = time
        }
        if let classifier, canPredict, time - lastPrediction >= 0.25 {
            let width = Double(CVPixelBufferGetWidth(buffer)), height = Double(CVPixelBufferGetHeight(buffer))
            let eligible = vehicles.indices.filter { index in
                let vehicle = vehicles[index]
                guard ["car", "truck", "bus"].contains(vehicle.label), vehicle.confidence >= 0.40,
                      LeadVehicle.validBox(vehicle.box),
                      Double(vehicle.box.width) * width >= 32, Double(vehicle.box.height) * height >= 32,
                      Double(vehicle.box.minY) < 0.62 else { return false }
                let y = Double(vehicle.box.minY)
                let bounds = lane?.bounds(at: y)
                let center = bounds.map { ($0.left + $0.right) / 2 } ?? 0.5
                let half = bounds.map { ($0.right - $0.left) / 2 } ?? LeadCorridor().halfWidth(at: y) ?? 0.05
                return abs(Double(vehicle.box.midX) - center) <= half * 1.45 + 0.02
            }.sorted { a, b in
                priority(vehicles[a], cached: linked[a].map { tracks[$0].viewpoint }, at: time, preferred: preferredBox)
                    > priority(vehicles[b], cached: linked[b].map { tracks[$0].viewpoint }, at: time, preferred: preferredBox)
            }
            if let index = eligible.first {
                let vehicle = vehicles[index]
                lastPrediction = time
                schedule(classifier, vehicle: vehicle, buffer: buffer, at: time)
            }
        }
        let result = vehicles.enumerated().map { index, vehicle in
            LeadVehicle(box: vehicle.box, label: vehicle.label, confidence: vehicle.confidence,
                        viewpoint: linked[index].map { tracks[$0].viewpoint })
        }
        if classifier != nil, time - lastStatus >= 2 {
            lastStatus = time
            let front = result.filter { $0.viewpoint?.isFrontFacing(at: time) == true }.count
            let rear = result.filter { $0.viewpoint?.isRearFacing(at: time) == true }.count
            onStatus?("Front/rear check · \(front) facing camera · \(rear) rear views · \(result.count - front - rear) uncertain")
        }
        return result
    }

    private func schedule(_ classifier: MLModel, vehicle: VehicleDetection, buffer: CVPixelBuffer,
                          at time: TimeInterval) {
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
                    self.completed = Evidence(box: vehicle.box, label: vehicle.label, viewpoint: viewpoint, seenAt: time)
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

    private func priority(_ vehicle: VehicleDetection, cached: VehicleViewpoint?, at time: TimeInterval,
                          preferred: CGRect?) -> Double {
        let age = cached.map { max(0, time - $0.capturedAt) } ?? 1.25
        // Age matters more than an uncertain label, so a side-on car cannot
        // monopolize the classifier while a confirmed lead's evidence expires.
        return (cached == nil ? 1.0 : 0) + 3 * min(1.25, age)
            + (preferred.map { Self.overlap($0, vehicle.box) > 0.35 ? 0.8 : 0 } ?? 0)
            + 0.4 * (0.62 - Double(vehicle.box.minY)) + 0.2 * vehicle.confidence
    }

    private func makeInput(_ buffer: CVPixelBuffer, vehicle: CGRect) throws -> CVPixelBuffer {
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

    private static func matches(_ a: CGRect, _ b: CGRect) -> Bool {
        LeadVehicle.validBox(a) && LeadVehicle.validBox(b)
            && (0.70...1.45).contains(Double(a.width / b.width))
            && (0.70...1.45).contains(Double(a.height / b.height))
            && abs(a.midX - b.midX) < max(0.035, b.width * 0.40)
            && abs(a.midY - b.midY) < max(0.025, b.height * 0.40) && overlap(a, b) >= 0.40
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
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
        try FileManager.default.copyItem(at: MLModel.compileModel(at: source), to: destination)
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
