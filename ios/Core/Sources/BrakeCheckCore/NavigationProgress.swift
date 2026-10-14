import Foundation

public struct NavigationCoordinate: Equatable, Sendable {
    public let latitude: Double
    public let longitude: Double
    public init(latitude: Double, longitude: Double) {
        self.latitude = latitude; self.longitude = longitude
    }
    fileprivate var isValid: Bool {
        latitude.isFinite && longitude.isFinite
            && (-90...90).contains(latitude) && (-180...180).contains(longitude)
    }
}

public struct NavigationProgressSnapshot: Equatable, Sendable {
    public let progressMeters: Double
    public let remainingMeters: Double
    public let distanceFromRoute: Double
    /// Index of the instruction at the next segment boundary. The initial
    /// heading instruction is shown briefly at the beginning of the route.
    public let stepIndex: Int
    public let distanceToStep: Double
    public let offRoute: Bool
    public let arrived: Bool
    public let mapMatchConfidence: Double
    public let mapMatchAmbiguous: Bool
}

/// Projects fresh GPS fixes onto a route without jumping between crossing
/// segments. Geometric lengths are scaled to the provider's route distance;
/// GPS uncertainty never renews progress or supplies arrival by itself.
public struct NavigationProgressTracker {
    private struct Vector {
        let x: Double
        let y: Double
        var length: Double { hypot(x, y) }
    }
    private struct Segment {
        let start: NavigationCoordinate
        let delta: Vector
        let length: Double
        let offset: Double
        let longitudeScale: Double
    }
    private struct Projection {
        let along: Double
        let distance: Double
        let headingError: Double
    }
    private struct StepEnd {
        let meters: Double
        let index: Int
    }
    private let segments: [Segment]
    private let geometryDistance: Double
    private let routeDistance: Double
    private let stepEnds: [StepEnd]
    private let stepCount: Int
    private let destination: NavigationCoordinate?
    private let valid: Bool
    private var lastTimestamp: Double?
    private var lastFix: NavigationCoordinate?
    private var lastMatchedFix: NavigationCoordinate?
    private var lastAccuracy = 0.0
    private var lastProjectionTime: Double?
    private var initialAlong: Double?
    private var along = 0.0
    private var offRoute = false
    private var outsideCount = 0
    private var outsideSince: Double?
    private var recoveryCount = 0
    private var recoverySince: Double?
    private var arrivalCount = 0
    private var arrivalSince: Double?
    private var arrived = false

    public init(coordinates: [NavigationCoordinate], routeDistance: Double, stepDistances: [Double]) {
        var built: [Segment] = []
        var distance = 0.0
        let inputsValid = coordinates.count >= 2 && coordinates.allSatisfy(\.isValid)
            && routeDistance.isFinite && routeDistance > 0
            && stepDistances.allSatisfy { $0.isFinite && $0 >= 0 }
            && stepDistances.reduce(0, +).isFinite
        if inputsValid {
            for (start, end) in zip(coordinates, coordinates.dropFirst()) {
                let scale = cos((start.latitude + end.latitude) * .pi / 360)
                let delta = Self.vector(from: start, to: end, longitudeScale: scale)
                let length = delta.length
                if length > 0.01 {
                    built.append(Segment(start: start, delta: delta, length: length,
                                         offset: distance, longitudeScale: scale))
                    distance += length
                }
            }
        }
        segments = built; geometryDistance = distance
        self.routeDistance = inputsValid ? routeDistance : 0
        destination = inputsValid ? coordinates.last : nil
        valid = inputsValid && distance.isFinite && distance > 0.01
        stepCount = max(1, stepDistances.count)
        let stepTotal = inputsValid ? stepDistances.reduce(0, +) : 0
        var ends: [StepEnd] = []
        var cumulative = 0.0
        if stepTotal.isFinite && stepTotal > 0 {
            for (index, length) in stepDistances.enumerated() where length > 0 {
                cumulative += length
                ends.append(StepEnd(meters: min(routeDistance, cumulative / stepTotal * routeDistance), index: index))
            }
        }
        stepEnds = ends
    }

    public mutating func update(coordinate: NavigationCoordinate, horizontalAccuracy: Double,
                                timestamp: Double, now: Double, courseDegrees: Double? = nil,
                                courseAccuracy: Double? = nil, speed: Double? = nil) -> NavigationProgressSnapshot? {
        guard valid, coordinate.isValid, horizontalAccuracy.isFinite,
              (0...35).contains(horizontalAccuracy), timestamp.isFinite, now.isFinite,
              timestamp >= 0, timestamp <= now, now - timestamp <= 2.0,
              lastTimestamp.map({ timestamp > $0 }) ?? true else { return nil }
        // An isolated GPS teleport must not poison subsequent good fixes or
        // become a shortcut to a later segment of a loop.
        if let lastFix, let lastTimestamp {
            let uncertainty = max(25, lastAccuracy + horizontalAccuracy)
            guard Self.distance(lastFix, coordinate) <= 65 * (timestamp - lastTimestamp) + uncertainty else { return nil }
        }
        let course = courseDegrees.flatMap { value -> Double? in
            guard value.isFinite, (0..<360).contains(value), let accuracy = courseAccuracy,
                  accuracy.isFinite, (0...20).contains(accuracy) else { return nil }
            return value * .pi / 180
        }
        // Infer travel direction from the last accepted on-route position;
        // a noisy or off-route fix must not poison recovery heading.
        let movement = lastMatchedFix.map { Self.vector(from: $0, to: coordinate, longitudeScale: cos(coordinate.latitude * .pi / 180)) }
        let observedHeading = course ?? movement.flatMap { $0.length >= max(4, horizontalAccuracy) ? atan2($0.x, $0.y) : nil }
        let projections = segments.map { segment -> Projection in
            let relative = Self.vector(from: segment.start, to: coordinate, longitudeScale: segment.longitudeScale)
            let fraction = min(1, max(0, (relative.x * segment.delta.x + relative.y * segment.delta.y)
                                     / (segment.length * segment.length)))
            let delta = observedHeading.map { atan2(segment.delta.x, segment.delta.y) - $0 } ?? 0
            let error = abs(atan2(sin(delta), cos(delta)))
            return Projection(along: segment.offset + fraction * segment.length,
                              distance: hypot(relative.x - fraction * segment.delta.x,
                                              relative.y - fraction * segment.delta.y), headingError: error)
        }
        guard let nearest = projections.min(by: { $0.distance < $1.distance }) else { return nil }
        let onThreshold = max(20, min(45, horizontalAccuracy * 1.6))
        let outsideThreshold = max(35, horizontalAccuracy * 2 + 10)
        let uncertainty = max(4, horizontalAccuracy * 0.5)
        let reachable: [Projection]
        if let lastProjectionTime {
            let speedLimit = speed.flatMap { $0.isFinite && (0...65).contains($0) ? max(8, $0 + 8) : nil } ?? 65
            let advance = speedLimit * (timestamp - lastProjectionTime) + max(20, horizontalAccuracy + lastAccuracy)
            let reverse = max(15, horizontalAccuracy * 1.5)
            reachable = projections.filter { $0.along >= along - reverse && $0.along <= along + advance
                && (observedHeading == nil || $0.headingError < .pi / 3) }
        } else { reachable = projections.filter { observedHeading == nil || $0.headingError < .pi / 3 } }
        let closestDistance = reachable.map(\.distance).min() ?? .infinity
        let close = reachable.filter {
            $0.distance <= closestDistance + uncertainty
                && (closestDistance > onThreshold || $0.distance <= onThreshold)
        }
        let projected = close.min {
            if lastProjectionTime == nil {
                if observedHeading != nil { return $0.distance + 8 * $0.headingError < $1.distance + 8 * $1.headingError }
                return $0.along < $1.along
            }
            let first = abs($0.along - along), second = abs($1.along - along)
            return abs(first - second) <= 1e-6 ? $0.distance < $1.distance : first < second
        }
        let isNear = projected.map { $0.distance <= onThreshold } == true
        // A nearby but unreachable route segment is not permission to skip
        // the intervening route. Treat sustained discontinuity as off-route.
        let isOutside = nearest.distance > outsideThreshold
            || (nearest.distance <= onThreshold && !isNear)
        lastTimestamp = timestamp; lastFix = coordinate; lastAccuracy = horizontalAccuracy
        if isOutside {
            if outsideSince == nil { outsideSince = timestamp }
            outsideCount += 1; recoveryCount = 0; recoverySince = nil
            if outsideCount >= 3, timestamp - (outsideSince ?? timestamp) >= 1.0 - 1e-6 { offRoute = true }
        } else {
            outsideCount = 0; outsideSince = nil
            if isNear {
                if recoverySince == nil { recoverySince = timestamp }
                recoveryCount += 1
                if recoveryCount >= 2, timestamp - (recoverySince ?? timestamp) >= 0.5 - 1e-6 { offRoute = false }
            } else { recoveryCount = 0; recoverySince = nil }
        }
        if isNear, !offRoute, let projected {
            lastMatchedFix = coordinate
            if initialAlong == nil { initialAlong = projected.along }
            along = min(geometryDistance, max(along, projected.along))
            lastProjectionTime = timestamp
        }
        let arrivalRadius = max(12, min(20, horizontalAccuracy + 6))
        let advanced = initialAlong.map { along - $0 >= min(20, geometryDistance * 0.15) } ?? false
        let atEnd = along >= max(geometryDistance * 0.90, geometryDistance - arrivalRadius)
        let nearDestination = destination.map { Self.distance($0, coordinate) <= arrivalRadius } == true
        if isNear, !offRoute, horizontalAccuracy <= 15, advanced, atEnd, nearDestination {
            if arrivalSince == nil { arrivalSince = timestamp }
            arrivalCount += 1
            if arrivalCount >= 3, timestamp - (arrivalSince ?? timestamp) >= 1.0 - 1e-6 { arrived = true }
        } else { arrivalCount = 0; arrivalSince = nil }
        let progress = min(routeDistance, max(0, along / geometryDistance * routeDistance))
        let boundary = stepEnds.first { $0.meters > progress + 1e-6 } ?? stepEnds.last
        var instruction = min(stepCount - 1, (boundary?.index ?? 0) + 1)
        let headingDistance = min(15, (stepEnds.first?.meters ?? routeDistance) * 0.25)
        if progress <= headingDistance { instruction = 0 }
        let matchedDistance = projected?.distance ?? nearest.distance
        let ambiguous = projected.map { selected in reachable.contains {
            abs($0.along - selected.along) > max(25, horizontalAccuracy * 3)
                && $0.distance <= selected.distance + max(3, horizontalAccuracy * 0.5)
                && $0.headingError <= selected.headingError + .pi / 12
        } } ?? true
        let matchConfidence = isNear && !offRoute && !ambiguous
            ? max(0, min(1, 1 - matchedDistance / max(20, horizontalAccuracy * 2) - horizontalAccuracy / 100
                         - (projected?.headingError ?? 0) / .pi * 0.4)) : 0
        return NavigationProgressSnapshot(progressMeters: progress, remainingMeters: max(0, routeDistance - progress),
            distanceFromRoute: matchedDistance, stepIndex: instruction,
            distanceToStep: max(0, (boundary?.meters ?? routeDistance) - progress),
            offRoute: offRoute, arrived: arrived, mapMatchConfidence: matchConfidence, mapMatchAmbiguous: ambiguous)
    }

    private static func vector(from start: NavigationCoordinate, to end: NavigationCoordinate,
                               longitudeScale: Double) -> Vector {
        let longitude = (end.longitude - start.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
        let radians = Double.pi / 180
        return Vector(x: longitude * radians * 6_371_008.8 * longitudeScale,
                      y: (end.latitude - start.latitude) * radians * 6_371_008.8)
    }
    private static func distance(_ a: NavigationCoordinate, _ b: NavigationCoordinate) -> Double {
        vector(from: a, to: b, longitudeScale: cos((a.latitude + b.latitude) * .pi / 360)).length
    }
}
