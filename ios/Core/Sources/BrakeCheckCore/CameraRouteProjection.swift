import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

public struct CameraRouteCalibration {
    public var heightMeters: Double
    public var horizonOffset: Double
    public var yawDegrees: Double
    public var referenceHorizon: Double?
    public init(heightMeters: Double = 1.4, horizonOffset: Double = 0, yawDegrees: Double = 0, referenceHorizon: Double? = nil) {
        self.heightMeters = heightMeters; self.horizonOffset = horizonOffset; self.yawDegrees = yawDegrees
        self.referenceHorizon = referenceHorizon
    }
}

/// Ground-plane approximation for a fixed, forward-facing camera mount.
/// Lane width convergence estimates the horizon (pitch); FOV and mount height
/// supply scale. Route progress selects the correct branch at crossings.
/// Only current road-supported portions are returned; missing observations
/// never generate a generic centered navigation trapezoid.
public enum CameraRouteProjection {
    private struct Vector {
        var east: Double
        var north: Double
        var length: Double { hypot(east, north) }
    }
    public static func project(route: [NavigationCoordinate], progressFraction: Double,
                               courseDegrees: Double, horizontalFOV: Double, aspect: Double,
                               calibration: CameraRouteCalibration, lane: LanePath?, road: RoadRegion?,
                               excluding boxes: [CGRect] = [], at time: TimeInterval) -> [[LaneBand]] {
        guard route.count >= 2, route.allSatisfy({
            $0.latitude.isFinite && $0.longitude.isFinite && (-90...90).contains($0.latitude)
                && (-180...180).contains($0.longitude)
        }), progressFraction.isFinite, (0...1).contains(progressFraction),
              courseDegrees.isFinite, (0..<360).contains(courseDegrees),
              horizontalFOV.isFinite, (20...150).contains(horizontalFOV),
              aspect.isFinite, (0.3...3).contains(aspect),
              calibration.heightMeters.isFinite, (0.5...3).contains(calibration.heightMeters),
              calibration.horizonOffset.isFinite, abs(calibration.horizonOffset) <= 0.15,
              calibration.yawDegrees.isFinite, abs(calibration.yawDegrees) <= 30,
              let lane, lane.isUsable(at: time), let road, road.isUsable(at: time) else { return [] }
        let n = Double(lane.bands.count)
        let meanY = lane.bands.reduce(0) { $0 + $1.y } / n
        let meanW = lane.bands.reduce(0) { $0 + $1.right - $1.left } / n
        let variance = lane.bands.reduce(0) { $0 + pow($1.y - meanY, 2) }
        guard variance > 0.001 else { return [] }
        let slope = lane.bands.reduce(0) { $0 + ($1.y - meanY) * ($1.right - $1.left - meanW) } / variance
        guard slope < -0.1 else { return [] }
        // A verified mount's horizon remains stable when observed lane width
        // fluctuates or the road curves. Never estimate height from lane width.
        let estimatedHorizon = calibration.referenceHorizon ?? (meanY - meanW / slope)
        guard (0.35...0.9).contains(estimatedHorizon) else { return [] }
        let horizon = estimatedHorizon + calibration.horizonOffset
        let fx = 0.5 / tan(horizontalFOV * .pi / 360)
        let fy = fx * aspect
        let heading = (courseDegrees + calibration.yawDegrees) * .pi / 180
        let origin = route[0]
        let scale = cos(origin.latitude * .pi / 180)
        let points = route.map { Vector(east: ($0.longitude - origin.longitude) * 111_320 * scale,
                                       north: ($0.latitude - origin.latitude) * 111_320) }
        // RoadRegion.bounds(at:) creates and sorts a LanePath on every call.
        // Projection samples up to 160 points per frame, so build it once.
        let pavementPath = LanePath(bands: road.bands, confidence: road.confidence, capturedAt: road.capturedAt)
        var distances = [0.0]
        for (a, b) in zip(points, points.dropFirst()) {
            distances.append(distances.last! + Vector(east: b.east - a.east, north: b.north - a.north).length)
        }
        guard let total = distances.last, total > 1, total.isFinite else { return [] }
        let progress = total * progressFraction
        func point(at distance: Double) -> Vector? {
            guard distance <= total else { return nil }
            var low = 1, high = points.count - 1
            while low < high {
                let middle = (low + high) / 2
                if distances[middle] < distance { low = middle + 1 } else { high = middle }
            }
            var index = low
            while index < points.count && distances[index] - distances[index - 1] <= 0.001 { index += 1 }
            guard index < points.count else { return nil }
            let length = distances[index] - distances[index - 1]
            let t = (distance - distances[index - 1]) / length
            return Vector(east: points[index - 1].east + (points[index].east - points[index - 1].east) * t,
                          north: points[index - 1].north + (points[index].north - points[index - 1].north) * t)
        }
        guard let start = point(at: progress), let ahead = point(at: min(total, progress + 5)) else { return [] }
        let tangent = Vector(east: ahead.east - start.east, north: ahead.north - start.north)
        guard tangent.length > 0.5 else { return [] }
        let headingError = atan2(tangent.east, tangent.north) - courseDegrees * .pi / 180
        guard abs(atan2(sin(headingError), cos(headingError))) < .pi / 6 else { return [] }
        var groups: [[LaneBand]] = []
        var current: [LaneBand] = []
        var alignment: Double?
        func finish() {
            if current.count >= 3 { groups.append(current.sorted { $0.y < $1.y }) }
            current = []
        }
        // Half-meter samples keep the ribbon continuous around tight turns.
        for index in 4...160 {
            guard let target = point(at: progress + Double(index) * 0.5) else { break }
            let east = target.east - start.east, north = target.north - start.north
            let forward = east * sin(heading) + north * cos(heading)
            let lateral = east * cos(heading) - north * sin(heading)
            guard forward > 1 else { finish(); continue }
            let y = horizon - fy * calibration.heightMeters / forward
            guard (0.04...0.85).contains(y), let pavement = pavementPath.bounds(at: y) else { finish(); continue }
            let rawX = 0.5 + fx * lateral / forward
            if alignment == nil, let bounds = lane.bounds(at: y) {
                alignment = (bounds.left + bounds.right) / 2 - rawX
            }
            guard let alignment, abs(alignment) <= 0.25 else { finish(); continue }
            // Map geometry can veto the corridor but cannot select an adjacent
            // lane or manufacture a branch through an intersection. Center on
            // the observed ego corridor, with a small map-agreement tolerance.
            guard let bounds = lane.bounds(at: y) else { finish(); continue }
            let center = (bounds.left + bounds.right) / 2
            guard abs(rawX + alignment - center) <= max(0.025, (bounds.right - bounds.left) * 0.45) else { return [] }
            let x = center
            let halfWidth = min(0.075, fx * 0.55 / forward, (bounds.right - bounds.left) * 0.22)
            let left = max(pavement.left + 0.008, bounds.left + 0.008, x - halfWidth)
            let right = min(pavement.right - 0.008, bounds.right - 0.008, x + halfWidth)
            guard left < right, x >= left, x <= right,
                  !boxes.contains(where: { box in
                      y >= Double(box.minY) - 0.01 && y <= Double(box.maxY) + 0.01
                          && right >= Double(box.minX) && left <= Double(box.maxX)
                  }) else { finish(); continue }
            if let mask = road.visibility, !(0...6).allSatisfy({ sample in
                mask.contains(x: left + (right - left) * Double(sample) / 6, y: y)
            }) { finish(); continue }
            // Do not bridge segments that fold back toward the camera.
            if let previous = current.last, y <= previous.y { finish() }
            current.append(LaneBand(y: y, left: left, right: right))
        }
        finish()
        return groups
    }
}

/// Smooth only consecutive fresh observations. Loss of support immediately
/// clears history; smoothing cannot renew a road mask's measurement time.
public struct CameraRouteSmoother {
    private var previous: [[LaneBand]] = []
    private var lastTime: TimeInterval?
    public init() {}
    public mutating func update(_ groups: [[LaneBand]], at time: TimeInterval, lane: LanePath? = nil, road: RoadRegion? = nil) -> [[LaneBand]] {
        let groups = groups.filter { !$0.isEmpty }
        guard !groups.isEmpty, time.isFinite else { previous = []; lastTime = nil; return [] }
        let dt = lastTime.map { time - $0 } ?? 1
        guard dt > 0, dt <= 0.3 else { previous = groups; lastTime = time; return groups }
        let alpha = 1 - exp(-dt / 0.12)
        // Cache interpolation paths for this draw instead of sorting the same
        // previous group and road bands once for every ribbon sample.
        let previousPaths = previous.compactMap { group -> (first: Double, last: Double, path: LanePath)? in
            guard let first = group.first, let last = group.last else { return nil }
            return (first.y, last.y, LanePath(bands: group, confidence: 1, capturedAt: time))
        }
        let roadPath = road.map { LanePath(bands: $0.bands, confidence: $0.confidence, capturedAt: $0.capturedAt) }
        let result = groups.map { group in group.map { band -> LaneBand in
            guard let oldGroup = previousPaths.first(where: { $0.first <= band.y && $0.last >= band.y }),
                  let old = oldGroup.path.bounds(at: band.y),
                  abs(old.left - band.left) < 0.06, abs(old.right - band.right) < 0.06 else { return band }
            var center = ((old.left + old.right) * (1 - alpha) + (band.left + band.right) * alpha) / 2
            var lower = band.left, upper = band.right
            if let corridor = lane?.bounds(at: band.y), let pavement = roadPath?.bounds(at: band.y) {
                lower = max(corridor.left + 0.008, pavement.left + 0.008)
                upper = min(corridor.right - 0.008, pavement.right - 0.008)
                let midpoint = (corridor.left + corridor.right) / 2
                let tolerance = (corridor.right - corridor.left) * 0.15
                center = min(midpoint + tolerance, max(midpoint - tolerance, center))
            }
            let half = min((band.right - band.left) / 2, center - lower, upper - center)
            guard half > 0 else { return band }
            if let mask = road?.visibility, !(0...6).allSatisfy({
                mask.contains(x: center - half + 2 * half * Double($0) / 6, y: band.y)
            }) { return band }
            return LaneBand(y: band.y, left: center - half, right: center + half)
        } }
        previous = result; lastTime = time
        return result
    }
}
