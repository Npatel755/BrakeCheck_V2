import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Raw semantic road membership, before span closing/vehicle-hole bridging.
/// This mask is exclusively for AR rendering, never collision eligibility.
public struct RoadVisibilityMask {
    public let width: Int
    public let height: Int
    public let pixels: [UInt8]
    public init?(width: Int, height: Int, pixels: [UInt8]) {
        guard width >= 8, height >= 8, width <= 640, height <= 640,
              pixels.count == width * height else { return nil }
        self.width = width; self.height = height; self.pixels = pixels
    }
    public func contains(x: Double, y: Double) -> Bool {
        guard x.isFinite, y.isFinite, (0...1).contains(x), (0...1).contains(y) else { return false }
        let column = min(width - 1, Int(x * Double(width)))
        let row = min(height - 1, Int((1 - y) * Double(height)))
        // Require neighboring pixels too: low-resolution mask edges must not
        // leak a cyan stripe onto a foreground obstacle or the sidewalk.
        for dy in -1...1 { for dx in -1...1 {
            let xx = column + dx, yy = row + dy
            guard xx >= 0, xx < width, yy >= 0, yy < height,
                  pixels[yy * width + xx] != 0 else { return false }
        } }
        return true
    }
}

public struct CameraARScene {
    public let lane: LanePath?
    public let road: RoadRegion?
    /// Raw model pixels for diagnostics only; never used as route support.
    public let diagnosticRoadMask: RoadVisibilityMask?
    public let laneObservedAt: TimeInterval
    public let roadObservedAt: TimeInterval
    public let capturedAt: TimeInterval
    public let obstacles: [CGRect]
    public let obstaclesFresh: Bool
    public let isPredicted: Bool
    public init(lane: LanePath?, road: RoadRegion?, laneObservedAt: TimeInterval, roadObservedAt: TimeInterval,
                capturedAt: TimeInterval, obstacles: [CGRect] = [], obstaclesFresh: Bool = true,
                isPredicted: Bool = false, diagnosticRoadMask: RoadVisibilityMask? = nil) {
        self.lane = lane; self.road = road; self.laneObservedAt = laneObservedAt
        self.diagnosticRoadMask = diagnosticRoadMask
        self.roadObservedAt = roadObservedAt; self.capturedAt = capturedAt
        self.obstacles = obstacles; self.obstaclesFresh = obstaclesFresh
        self.isPredicted = isPredicted
    }
}

/// Brief constant-velocity continuation from two visually aligned scenes.
/// Predicted scenes never enter history, so missing detections cannot accumulate
/// drift or prolong the 180 ms prediction horizon. A new negative road inference
/// invalidates the history immediately rather than being treated as a dropout.
public struct CameraARSceneTracker {
    private var previous: CameraARScene?
    private var latest: CameraARScene?
    public init() {}
    public mutating func reset() { previous = nil; latest = nil }
    public mutating func update(_ observation: CameraARScene, at time: TimeInterval) -> CameraARScene {
        guard time.isFinite, time >= observation.capturedAt, time - observation.capturedAt <= 0.1 else { reset(); return observation }
        if observation.road == nil, let latest, observation.roadObservedAt > latest.roadObservedAt {
            reset(); return observation
        }
        let aligned = observation.lane.map { $0.isUsable(at: time) && abs($0.capturedAt - time) < 0.06 && $0.source != .markings } == true
            && observation.road.map { $0.isUsable(at: time) && abs($0.capturedAt - time) < 0.06 } == true
        if aligned {
            if latest.map({ observation.capturedAt > $0.capturedAt }) ?? true { previous = latest; latest = observation }
            return observation
        }
        guard let latest, let previous, let lane = latest.lane, let oldLane = previous.lane, let road = latest.road,
              time > latest.capturedAt, time - latest.capturedAt <= 0.18,
              time - latest.laneObservedAt <= 0.8, time - latest.roadObservedAt <= 0.8 else { return observation }
        let dt = latest.capturedAt - previous.capturedAt, future = time - latest.capturedAt
        guard (0.03...0.3).contains(dt) else { return observation }
        var supported = true
        let bands = lane.bands.map { band -> LaneBand in
            guard let old = oldLane.bounds(at: band.y) else { supported = false; return band }
            let leftVelocity = (band.left - old.left) / dt, rightVelocity = (band.right - old.right) / dt
            guard abs(leftVelocity) <= 0.15, abs(rightVelocity) <= 0.15 else { supported = false; return band }
            return LaneBand(y: band.y, left: band.left + leftVelocity * future, right: band.right + rightVelocity * future)
        }
        guard supported else { return observation }
        let predictedLane = LanePath(bands: bands, confidence: lane.confidence - 0.10 - future * 0.15,
                                     capturedAt: time, source: lane.source)
        guard let predictedRoad = RoadVisualProjection.project(road, from: lane, to: predictedLane, at: time) else { return observation }
        let reducedRoad = RoadRegion(bands: predictedRoad.bands, confidence: predictedRoad.confidence - 0.10,
                                    capturedAt: time, visibility: predictedRoad.visibility)
        return CameraARScene(lane: predictedLane, road: reducedRoad, laneObservedAt: latest.laneObservedAt,
            roadObservedAt: latest.roadObservedAt, capturedAt: time, obstacles: observation.obstacles,
            obstaclesFresh: observation.obstaclesFresh, isPredicted: true,
            diagnosticRoadMask: observation.diagnosticRoadMask)
    }
}

/// Warp back to the original model frame through a visually tracked lane.
/// Prediction is bounded and never recursively renews a segmentation timestamp.
public enum RoadVisualProjection {
    public static func project(_ road: RoadRegion, from original: LanePath, to tracked: LanePath,
                               at time: TimeInterval) -> RoadRegion? {
        guard time.isFinite, time >= road.capturedAt, time - road.capturedAt <= 0.8,
              original.isUsable(at: road.capturedAt), tracked.isUsable(at: time),
              let a = original.bands.first, let b = original.bands.last,
              let c = tracked.bands.first, let d = tracked.bands.last,
              b.y - a.y > 0.1, d.y - c.y > 0.1 else { return nil }
        let scaleY = (d.y - c.y) / (b.y - a.y)
        guard (0.8...1.2).contains(scaleY) else { return nil }
        func inverse(_ x: Double, _ y: Double) -> (Double, Double)? {
            let oldY = a.y + (y - c.y) / scaleY
            guard let old = original.bounds(at: oldY), let new = tracked.bounds(at: y) else { return nil }
            let scaleX = (new.right - new.left) / (old.right - old.left)
            guard (0.75...1.3).contains(scaleX), abs((new.left + new.right - old.left - old.right) / 2) < 0.08 else { return nil }
            return ((x - (new.left + new.right) / 2) / scaleX + (old.left + old.right) / 2, oldY)
        }
        let bands = road.bands.compactMap { band -> LaneBand? in
            let y = c.y + (band.y - a.y) * scaleY
            guard (0...1).contains(y), let old = original.bounds(at: band.y), let new = tracked.bounds(at: y) else { return nil }
            let scaleX = (new.right - new.left) / (old.right - old.left)
            guard (0.75...1.3).contains(scaleX), abs((new.left + new.right - old.left - old.right) / 2) < 0.08 else { return nil }
            let center = (new.left + new.right) / 2
            return LaneBand(y: y, left: max(0, center + (band.left - (old.left + old.right) / 2) * scaleX),
                            right: min(1, center + (band.right - (old.left + old.right) / 2) * scaleX))
        }
        var mask: RoadVisibilityMask?
        if let source = road.visibility {
            var pixels = [UInt8](repeating: 0, count: source.width * source.height)
            for row in 0..<source.height {
                let y = 1 - (Double(row) + 0.5) / Double(source.height)
                // Lane bounds and y mapping depend on the row, not the pixel.
                // Compute the affine x mapping twice per row instead of doing
                // two lane-bound searches for every one of 15,360 pixels.
                guard let start = inverse(0, y), let end = inverse(1, y) else { continue }
                let scale = end.0 - start.0
                for column in 0..<source.width {
                    let x = (Double(column) + 0.5) / Double(source.width)
                    if source.contains(x: start.0 + x * scale, y: start.1) { pixels[row * source.width + column] = 1 }
                }
            }
            mask = RoadVisibilityMask(width: source.width, height: source.height, pixels: pixels)
        }
        let result = RoadRegion(bands: bands, confidence: road.confidence - 0.10 * (time - road.capturedAt),
                                capturedAt: time, visibility: mask)
        return result.isUsable(at: time) ? result : nil
    }
}

public struct CameraCalibrationEstimate: Equatable, Codable {
    public let horizon: Double
    public let yawDegrees: Double
    public let pitchDegrees: Double
    public let confidence: Double
}

public enum CameraCalibrationGeometry {
    public static func estimate(lane: LanePath, fov: Double, aspect: Double) -> CameraCalibrationEstimate? {
        guard fov.isFinite, (20...150).contains(fov), aspect.isFinite, (0.3...3).contains(aspect),
              lane.isUsable(at: lane.capturedAt), lane.confidence >= 0.7 else { return nil }
        let bands = lane.bands, n = Double(bands.count)
        let yMean = bands.reduce(0) { $0 + $1.y } / n
        let wMean = bands.reduce(0) { $0 + $1.right - $1.left } / n
        let xMean = bands.reduce(0) { $0 + ($1.left + $1.right) / 2 } / n
        let variance = bands.reduce(0) { $0 + pow($1.y - yMean, 2) }
        guard variance > 0.005 else { return nil }
        let slope = bands.reduce(0) { $0 + ($1.y - yMean) * ($1.right - $1.left - wMean) } / variance
        let centerSlope = bands.reduce(0) { $0 + ($1.y - yMean) * (($1.left + $1.right) / 2 - xMean) } / variance
        guard slope < -0.1 else { return nil }
        let residual = sqrt(bands.reduce(0) { $0 + pow($1.right - $1.left - (wMean + slope * ($1.y - yMean)), 2) } / n)
        let centerResidual = sqrt(bands.reduce(0) { $0 + pow(($1.left + $1.right) / 2 - (xMean + centerSlope * ($1.y - yMean)), 2) } / n)
        guard residual < 0.02, centerResidual < 0.012 else { return nil }
        let horizon = yMean - wMean / slope
        let vanishingX = xMean + centerSlope * (horizon - yMean)
        guard (0.35...0.9).contains(horizon), (0.25...0.75).contains(vanishingX) else { return nil }
        let fx = 0.5 / tan(fov * .pi / 360)
        return CameraCalibrationEstimate(horizon: horizon, yawDegrees: -atan((vanishingX - 0.5) / fx) * 180 / .pi,
            pitchDegrees: atan((horizon - 0.5) / (fx * aspect)) * 180 / .pi,
            confidence: max(0, min(lane.confidence, 1 - residual * 8 - centerResidual * 12)))
    }
}

/// Distinct observations must agree; repeated UI refreshes cannot establish
/// calibration. Curves and unstable frames deliberately produce no suggestion.
public struct CameraCalibrationAssistant {
    private var samples: [(TimeInterval, CameraCalibrationEstimate)] = []
    public var observationCount: Int { samples.count }
    public init() {}
    public mutating func update(_ estimate: CameraCalibrationEstimate?, observedAt time: TimeInterval) -> CameraCalibrationEstimate? {
        guard time.isFinite else { samples = []; return nil }
        guard let estimate else {
            // Cadence skips and brief occlusions cannot erase all progress.
            // They supply no new evidence and never renew a ready estimate.
            if let last = samples.last, time >= last.0, time - last.0 <= 1.2 {
                return time - last.0 <= 0.8 ? suggestion() : nil
            }
            samples = []; return nil
        }
        guard [estimate.horizon, estimate.yawDegrees, estimate.pitchDegrees, estimate.confidence].allSatisfy(\.isFinite),
              estimate.confidence >= 0.7 else { samples = []; return nil }
        if samples.last.map({ time > $0.0 }) ?? true {
            samples.removeAll { time - $0.0 > 4 }
            if let prior = samples.last?.1, abs(prior.horizon - estimate.horizon) > 0.04 || abs(prior.yawDegrees - estimate.yawDegrees) > 3 { samples = [] }
            samples.append((time, estimate))
        }
        return suggestion()
    }
    private func suggestion() -> CameraCalibrationEstimate? {
        guard samples.count >= 8, let first = samples.first, let last = samples.last,
              last.0 - first.0 >= 1 else { return nil }
        let count = Double(samples.count)
        let horizon = samples.reduce(0) { $0 + $1.1.horizon } / count
        let yaw = samples.reduce(0) { $0 + $1.1.yawDegrees } / count
        guard samples.allSatisfy({ abs($0.1.horizon - horizon) <= 0.02 && abs($0.1.yawDegrees - yaw) <= 1.5 }) else { return nil }
        return CameraCalibrationEstimate(horizon: horizon, yawDegrees: yaw,
            pitchDegrees: samples.reduce(0) { $0 + $1.1.pitchDegrees } / count,
            confidence: samples.map { $0.1.confidence }.min()!)
    }
}

public enum CameraARConfidence: String { case aligned, directional, lost }
public struct CameraARConfidenceTracker {
    private var lastTime: TimeInterval?
    private var alignedSince: TimeInterval?
    private var count = 0
    public init() {}
    public mutating func update(_ requested: CameraARConfidence, observedAt time: TimeInterval) -> CameraARConfidence {
        guard requested == .aligned, time.isFinite else { count = 0; alignedSince = nil; lastTime = nil; return requested }
        if lastTime.map({ time > $0 }) ?? true {
            if lastTime.map({ time - $0 > 0.35 }) ?? true { count = 0; alignedSince = time }
            count += 1; lastTime = time
        }
        return count >= 3 && time - (alignedSince ?? time) >= 0.2 ? .aligned : .directional
    }
}
public enum CameraMountVerification {
    public static func signature(width: Int, height: Int, fov: Double?, orientation: Int = 0, calibration: CameraRouteCalibration) -> String {
        guard let fov, fov.isFinite, width > 0, height > 0 else { return "unavailable" }
        return String(format: "%dx%d:%d:%.2f:%.3f:%.3f:%.3f", width, height, orientation, fov,
            calibration.heightMeters, calibration.horizonOffset, calibration.yawDegrees)
    }
}
public enum CameraARConfidencePolicy {
    public static func level(scene: CameraARScene?, verified: Bool, routeMatched: Bool,
                             navigationReliable: Bool, branchAmbiguous: Bool, at time: TimeInterval) -> CameraARConfidence {
        guard time.isFinite, navigationReliable, let scene,
              time >= scene.capturedAt, time - scene.capturedAt <= 0.35 else { return .lost }
        let laneSupported = scene.lane.map { $0.isUsable(at: time) } == true
            && time >= scene.laneObservedAt && time - scene.laneObservedAt <= 0.8
        let roadSupported = scene.road.map { $0.isUsable(at: time) } == true
            && time >= scene.roadObservedAt && time - scene.roadObservedAt <= 0.8
        guard laneSupported || roadSupported else { return .lost }
        guard laneSupported && roadSupported, let lane = scene.lane, let road = scene.road else { return .directional }
        guard verified, routeMatched, !branchAmbiguous, scene.obstaclesFresh,
              abs(scene.capturedAt - road.capturedAt) <= 0.1,
              abs(scene.capturedAt - lane.capturedAt) <= 0.1,
              lane.confidence >= 0.75, road.confidence >= 0.75,
              lane.source != .markings, road.visibility != nil else { return .directional }
        return .aligned
    }
}
