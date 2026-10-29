import Foundation

/// Geometry for browsing places along a route. This never updates measured
/// progress, guidance, arrival, or camera/safety measurements.
public enum NavigationRemainingRouteGeometry {
    /// Measured route progress wins over geographic proximity at loops and
    /// crossings. During planning only, a nearby origin can trim a partial edge.
    /// Without either, keep the full route rather than guess the driver's branch.
    public static func remainingRoute(_ coordinates: [NavigationCoordinate],
        measuredProgressFraction: Double? = nil, origin: NavigationCoordinate? = nil) -> [NavigationCoordinate] {
        guard let lengths = lengths(coordinates), let end = coordinates.last else { return [] }
        let total = lengths.reduce(0, +)
        guard total > 0 else { return [end] }
        if let fraction = measuredProgressFraction {
            guard fraction.isFinite, (0...1).contains(fraction) else { return coordinates }
            return suffix(coordinates, lengths: lengths, distance: total * fraction)
        }
        guard let origin, origin.isValid else { return coordinates }
        var nearestDistance = Double.infinity
        var nearestAlong = 0.0
        var offset = 0.0
        for index in lengths.indices {
            let start = coordinates[index], finish = coordinates[index + 1]
            defer { offset += lengths[index] }
            guard lengths[index] > 0 else { continue }
            let scale = max(1e-9, cos((start.latitude + finish.latitude) * .pi / 360))
            let dx = longitudeDelta(from: start.longitude, to: finish.longitude) * scale
            let dy = finish.latitude - start.latitude
            let px = longitudeDelta(from: start.longitude, to: origin.longitude) * scale
            let py = origin.latitude - start.latitude
            let fraction = min(1, max(0, (px * dx + py * dy) / (dx * dx + dy * dy)))
            let point = interpolate(start, finish, fraction: fraction)
            let distance = point.distance(to: origin)
            // Tied crossings keep the earliest branch; only measured progress
            // can authorize discarding an overlapping section of a route.
            if distance < nearestDistance - 1e-6 {
                nearestDistance = distance
                nearestAlong = offset + lengths[index] * fraction
            }
        }
        guard nearestDistance <= 250 else { return coordinates }
        return suffix(coordinates, lengths: lengths, distance: nearestAlong)
    }

    /// Sample physical route distance, not vertex index, so sparse highways and
    /// densely drawn junctions get the same search coverage.
    public static func searchCenters(_ coordinates: [NavigationCoordinate],
        fractions: [Double] = [0, 0.45, 0.85]) -> [NavigationCoordinate] {
        guard let lengths = lengths(coordinates), let first = coordinates.first else { return [] }
        let total = lengths.reduce(0, +)
        guard total > 0 else { return [first] }
        return fractions.compactMap { fraction in
            guard fraction.isFinite, (0...1).contains(fraction) else { return nil }
            let distance = total * fraction
            var offset = 0.0
            for index in lengths.indices {
                let length = lengths[index]
                if length > 0, distance < offset + length {
                    return interpolate(coordinates[index], coordinates[index + 1], fraction: (distance - offset) / length)
                }
                offset += length
            }
            return coordinates.last
        }
    }

    private static func lengths(_ coordinates: [NavigationCoordinate]) -> [Double]? {
        guard !coordinates.isEmpty, coordinates.allSatisfy(\.isValid) else { return nil }
        let result = zip(coordinates, coordinates.dropFirst()).map { $0.distance(to: $1) }
        return result.allSatisfy(\.isFinite) ? result : nil
    }

    private static func suffix(_ coordinates: [NavigationCoordinate], lengths: [Double], distance: Double) -> [NavigationCoordinate] {
        guard distance > 0 else { return coordinates }
        var offset = 0.0
        for index in lengths.indices {
            let length = lengths[index]
            if length > 0, distance < offset + length {
                let start = interpolate(coordinates[index], coordinates[index + 1], fraction: (distance - offset) / length)
                return [start] + coordinates[(index + 1)...]
            }
            offset += length
        }
        return coordinates.last.map { [$0] } ?? []
    }

    private static func longitudeDelta(from: Double, to: Double) -> Double {
        (to - from + 540).truncatingRemainder(dividingBy: 360) - 180
    }
    private static func interpolate(_ a: NavigationCoordinate, _ b: NavigationCoordinate, fraction: Double) -> NavigationCoordinate {
        NavigationCoordinate(latitude: a.latitude + (b.latitude - a.latitude) * fraction,
            longitude: (a.longitude + longitudeDelta(from: a.longitude, to: b.longitude) * fraction + 540)
                .truncatingRemainder(dividingBy: 360) - 180)
    }
}
