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
    private let modelQueue = DispatchQueue(label: "BrakeCheck.lane-model", qos: .userInitiated,
                                          autoreleaseFrequency: .workItem)
    private let stateLock = NSLock()
    private let inference: VisionInferenceCoordinator
    private var minimumInterval: TimeInterval = 0.20

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
    private var roadMeasurements: [(time: TimeInterval, region: RoadRegion?)] = []
    private var vehicleBoxes: [CGRect] = []
    private var boxesTime: TimeInterval = -.infinity

    // These properties are used exclusively on modelQueue.
    private var model: MLModel?
    private var neuralEstimator = NeuralLaneEstimator()
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var inputPool: CVPixelBufferPool?
    private var lastStatusTime: TimeInterval = -.infinity
    private var consecutiveErrors = 0
    private var inputWidth = 640
    private var inputHeight = 384

    // These properties are used exclusively on VehicleTracker's queue.
    private var estimator = LaneEstimator()
    private var lastScan: TimeInterval = -.infinity
    private var latest: LanePath?
    private(set) var roadRegion: RoadRegion?

    func prepare() {
        stateLock.lock()
        guard !loadingStarted else { stateLock.unlock(); return }
        loadingStarted = true
        stateLock.unlock()
        modelQueue.async {
            self.onStatus?("YOLOP lanes loading…")
            do {
                let source = try self.locateModel()
                let compiled = try self.compiledModel(from: source)
                let options = MLModelConfiguration()
                options.computeUnits = .cpuAndNeuralEngine
                let model: MLModel
                do { model = try MLModel(contentsOf: compiled, configuration: options) }
                catch {
                    // A interrupted/old compilation must not permanently force
                    // the fallback. Rebuild only this model's hashed cache.
                    try FileManager.default.removeItem(at: compiled)
                    model = try MLModel(contentsOf: self.compiledModel(from: source), configuration: options)
                }
                guard let image = model.modelDescription.inputDescriptionsByName["image"]?.imageConstraint,
                      (64...640).contains(image.pixelsWide), (64...640).contains(image.pixelsHigh),
                      model.modelDescription.outputDescriptionsByName["lane_scores"] != nil,
                      model.modelDescription.outputDescriptionsByName["road_scores"] != nil else {
                    throw LaneModelError.contract
                }
                self.inputWidth = image.pixelsWide; self.inputHeight = image.pixelsHigh
                self.model = model
                self.stateLock.lock(); self.modelReady = true; self.stateLock.unlock()
                self.onStatus?("YOLOP lane + road heads ready")
            } catch {
                self.stateLock.lock(); self.loadingStarted = false; self.stateLock.unlock()
                self.onStatus?("YOLOP lanes unavailable: \(error.localizedDescription) · paint fallback")
            }
        }
    }

    func reset() {
        estimator.reset(); latest = nil; roadRegion = nil; lastScan = -.infinity
        stateLock.lock()
        generation += 1
        neuralMeasurements.reset()
        roadMeasurements = []
        vehicleBoxes = []; boxesTime = -.infinity
        lastSubmission = -.infinity
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
        let road = roadMeasurements.last { $0.time <= time }?.region
        stateLock.unlock()
        roadRegion = road.flatMap { $0.isUsable(at: now) ? $0 : nil }
        // Keep both observers running. A complete paint section can extend a
        // matching short model section; the fallback no longer stays cold
        // while a partial neural lane monopolizes the output.
        let markings = scanMarkings(buffer, excluding: boxes, at: time)
        if let measurement, time >= measurement.lane.capturedAt,
           time - measurement.lane.capturedAt <= 1.0 {
            if let frame = lumaFrame(buffer, at: time),
               let tracked = LaneVisualTracker.project(measurement.lane, from: measurement.frame,
                                                       to: frame, excluding: boxes), tracked.isUsable(at: now) {
                return LaneFusion.combine(neural: tracked, markings: markings, at: now)
            }
            // A failed visual match does not renew the model's capture time.
            if measurement.lane.isUsable(at: now) {
                return LaneFusion.combine(neural: measurement.lane, markings: markings, at: now)
            }
        }
        return LaneFusion.combine(neural: nil, markings: markings, at: now)
    }

    /// Offered after vehicle inference/rear scheduling, or on a cadence-skipped
    /// capture. The independent model queue retains at most one frame.
    func submit(_ buffer: CVPixelBuffer, at time: TimeInterval) {
        stateLock.lock()
        guard modelReady, !modelBusy, time.isFinite, time - lastSubmission >= minimumInterval - 1e-6 else {
            stateLock.unlock(); return
        }
        modelBusy = true; lastSubmission = time
        let submittedGeneration = generation
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
                    let input = try self.makeInput(buffer)
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
                guard let scores = result.featureValue(for: "lane_scores")?.multiArrayValue,
                      let roadScores = result.featureValue(for: "road_scores")?.multiArrayValue,
                      let transform = LaneImageTransform(width: self.inputWidth, height: self.inputHeight,
                          sourceWidth: CVPixelBufferGetWidth(buffer), sourceHeight: CVPixelBufferGetHeight(buffer)) else {
                    throw LaneModelError.output
                }
                let road = try self.withScores(roadScores) { values in
                    RoadSegmentationScanner.scan(transform: transform, excluding: boxes, at: time) { x, y in
                        let value = values(x, y); return (value.background, value.foreground)
                    }
                }
                let rawRows = try self.decode(scores, transform: transform, excluding: boxes)
                let rows = road?.filter(rawRows, at: time) ?? rawRows
                let lane = self.neuralEstimator.update(rows, at: time)
                let finished = ProcessInfo.processInfo.systemUptime
                self.consecutiveErrors = 0
                self.stateLock.lock()
                if self.generation == submittedGeneration {
                    // Keep nil observations too: a failed current mask cannot
                    // silently revive an older road measurement.
                    self.roadMeasurements.removeAll { time - $0.time > 1.2 }
                    self.roadMeasurements.append((time, road))
                    if self.roadMeasurements.count > 6 { self.roadMeasurements.removeFirst() }
                    if let lane, abs(lane.capturedAt - time) < 0.001, finished - time <= 1.0,
                       let measurement = LaneModelMeasurement(lane: lane, frame: frame) {
                        self.neuralMeasurements.append(measurement)
                    }
                }
                self.stateLock.unlock()
                if finished - self.lastStatusTime >= 2 {
                    self.lastStatusTime = finished
                    let duration = Int((finished - started) * 1000)
                    let count = rows.filter { !$0.peaks.isEmpty }.count
                    self.onStatus?(finished - time > 1.0
                        ? "YOLOP · \(duration) ms · frame too old to track"
                        : "YOLOP lane + road · \(duration) ms · \(count) lane rows · road \(road?.bands.count ?? 0) rows · \(self.neuralEstimator.status)")
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
                self.onStatus?("YOLOP inference error: \(error.localizedDescription) · retrying · paint fallback")
            }
        }
    }

    private func makeInput(_ buffer: CVPixelBuffer) throws -> CVPixelBuffer {
        if inputPool == nil {
            let attributes: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: inputWidth,
                kCVPixelBufferHeightKey as String: inputHeight,
                kCVPixelBufferIOSurfacePropertiesKey as String: [:]
            ]
            guard CVPixelBufferPoolCreate(kCFAllocatorDefault, nil, attributes as CFDictionary, &inputPool) == kCVReturnSuccess else {
                throw LaneModelError.frame
            }
        }
        guard let inputPool else { throw LaneModelError.frame }
        var resized: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, inputPool, &resized) == kCVReturnSuccess,
              let resized else { throw LaneModelError.frame }
        let source = CIImage(cvPixelBuffer: buffer)
        guard let transform = LaneImageTransform(width: inputWidth, height: inputHeight,
                                                sourceWidth: CVPixelBufferGetWidth(buffer),
                                                sourceHeight: CVPixelBufferGetHeight(buffer)) else { throw LaneModelError.frame }
        let scale = CGFloat(transform.contentWidth / Double(CVPixelBufferGetWidth(buffer)))
        let rect = CGRect(x: 0, y: 0, width: CGFloat(inputWidth), height: CGFloat(inputHeight))
        let image = source.transformed(by: CGAffineTransform(translationX: -source.extent.minX, y: -source.extent.minY))
            .transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            .transformed(by: CGAffineTransform(translationX: CGFloat(transform.paddingX), y: CGFloat(transform.paddingY)))
        let background = CIImage(color: CIColor(red: 114.0 / 255, green: 114.0 / 255, blue: 114.0 / 255)).cropped(to: rect)
        context.render(image.composited(over: background).cropped(to: rect), to: resized,
                       bounds: rect, colorSpace: CGColorSpaceCreateDeviceRGB())
        return resized
    }

    private func decode(_ array: MLMultiArray, transform: LaneImageTransform,
                        excluding boxes: [CGRect]) throws -> [LaneMarkingRow] {
        try withScores(array) { values in
            NeuralLaneScanner.scan(transform: transform, excluding: boxes) { x, y in
                let value = values(x, y); return (value.background, value.foreground)
            }
        }
    }

    private func withScores<T>(_ array: MLMultiArray,
                               body: ((Int, Int) -> (background: Double, foreground: Double)) -> T) throws -> T {
        let shape = array.shape.map { $0.intValue }
        let strides = array.strides.map { $0.intValue }
        guard shape.count >= 3, Array(shape.suffix(3)) == [2, inputHeight, inputWidth],
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

    private func locateModel() throws -> URL {
        for bundle in [Bundle.module, Bundle.main] + Bundle.allBundles + Bundle.allFrameworks {
            if let source = bundle.url(forResource: "LaneModelData", withExtension: nil) {
                let model = source.appendingPathComponent("YOLOPLane.mlmodel")
                if FileManager.default.fileExists(atPath: model.path) { return model }
            }
            if let source = bundle.resourceURL?.appendingPathComponent("LaneModelData/YOLOPLane.mlmodel"),
               FileManager.default.fileExists(atPath: source.path) { return source }
            if let source = bundle.url(forResource: "YOLOPLane", withExtension: "mlmodel") { return source }
        }
        throw LaneModelError.missing
    }

    private func compiledModel(from source: URL) throws -> URL {
        // A content hash invalidates the on-device cache when model weights
        // change, while keeping first-launch compilation out of later trips.
        let digest = SHA256.hash(data: try Data(contentsOf: source)).map { String(format: "%02x", $0) }.joined()
        let directory = try FileManager.default.url(for: .cachesDirectory, in: .userDomainMask,
                                                    appropriateFor: nil, create: true)
            .appendingPathComponent("BrakeCheckLaneModels", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent("YOLOP-\(digest.prefix(16)).mlmodelc", isDirectory: true)
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
        case .missing: return "LaneModelData/YOLOPLane.mlmodel is missing"
        case .contract: return "expected YOLOP RGB input, lane_scores and road_scores outputs; install the new model file"
        case .frame: return "camera frame could not be resized"
        case .output: return "expected floating-point scores [1,2,height,width]"
        }
    }
}
