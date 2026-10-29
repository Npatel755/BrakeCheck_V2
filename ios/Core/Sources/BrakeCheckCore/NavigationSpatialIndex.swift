import Foundation

/// Conservative bounding-volume index. Exact road/route projection still decides
/// the match; boxes only remove geometry that cannot be within the search radius.
struct NavigationSpatialIndex {
    struct Bounds {
        let minLatitude: Double
        let maxLatitude: Double
        let minLongitude: Double
        let maxLongitude: Double

        init(coordinates: [NavigationCoordinate]) {
            minLatitude = coordinates.map(\.latitude).min() ?? 0
            maxLatitude = coordinates.map(\.latitude).max() ?? 0
            let wraps = zip(coordinates, coordinates.dropFirst()).contains { abs($0.longitude - $1.longitude) > 180 }
            minLongitude = wraps ? -180 : (coordinates.map(\.longitude).min() ?? 0)
            maxLongitude = wraps ? 180 : (coordinates.map(\.longitude).max() ?? 0)
        }
        private init(minLatitude: Double, maxLatitude: Double, minLongitude: Double, maxLongitude: Double) {
            self.minLatitude = minLatitude; self.maxLatitude = maxLatitude
            self.minLongitude = minLongitude; self.maxLongitude = maxLongitude
        }
        func union(_ other: Bounds) -> Bounds {
            Bounds(minLatitude: min(minLatitude, other.minLatitude), maxLatitude: max(maxLatitude, other.maxLatitude),
                minLongitude: min(minLongitude, other.minLongitude), maxLongitude: max(maxLongitude, other.maxLongitude))
        }
        func intersects(_ point: NavigationCoordinate, latitudeMargin: Double, longitudeMargin: Double) -> Bool {
            guard maxLatitude >= point.latitude - latitudeMargin, minLatitude <= point.latitude + latitudeMargin else { return false }
            // Query both sides of the date line when the search radius crosses it.
            for shift in [-360.0, 0, 360] {
                let longitude = point.longitude + shift
                if maxLongitude >= longitude - longitudeMargin && minLongitude <= longitude + longitudeMargin { return true }
            }
            return false
        }
    }
    private struct Entry { let index: Int; let bounds: Bounds }
    private struct Node { let bounds: Bounds; let range: Range<Int>; let left: Int?; let right: Int? }
    private let entries: [Entry]
    private let nodes: [Node]

    init(bounds: [Bounds]) {
        var entries = bounds.enumerated().map { Entry(index: $0.offset, bounds: $0.element) }
        var nodes = [Node](); nodes.reserveCapacity(max(1, bounds.count / 4))
        func build(_ range: Range<Int>) -> Int {
            var box = entries[range.lowerBound].bounds
            for index in range.dropFirst() { box = box.union(entries[index].bounds) }
            let node = nodes.count
            nodes.append(Node(bounds: box, range: range, left: nil, right: nil))
            if range.count > 8 {
                let latitude = box.maxLatitude - box.minLatitude >= box.maxLongitude - box.minLongitude
                entries.replaceSubrange(range, with: entries[range].sorted {
                    let first = latitude ? $0.bounds.minLatitude + $0.bounds.maxLatitude : $0.bounds.minLongitude + $0.bounds.maxLongitude
                    let second = latitude ? $1.bounds.minLatitude + $1.bounds.maxLatitude : $1.bounds.minLongitude + $1.bounds.maxLongitude
                    return first == second ? $0.index < $1.index : first < second
                })
                let middle = range.lowerBound + range.count / 2
                let left = build(range.lowerBound..<middle), right = build(middle..<range.upperBound)
                nodes[node] = Node(bounds: box, range: range, left: left, right: right)
            }
            return node
        }
        if !entries.isEmpty { _ = build(entries.indices) }
        self.entries = entries; self.nodes = nodes
    }

    func candidates(near point: NavigationCoordinate, radius: Double, minimumLongitudeScale: Double? = nil) -> [Int] {
        guard !nodes.isEmpty else { return [] }
        let latitudeMargin = radius / 110_000
        let longitudeScale = minimumLongitudeScale ?? abs(cos(min(90, abs(point.latitude) + latitudeMargin) * .pi / 180))
        let longitudeMargin = latitudeMargin / max(0.000_001, longitudeScale)
        var result = [Int](); result.reserveCapacity(16)
        func visit(_ index: Int) {
            let node = nodes[index]
            guard node.bounds.intersects(point, latitudeMargin: latitudeMargin, longitudeMargin: longitudeMargin) else { return }
            if let left = node.left, let right = node.right { visit(left); visit(right) }
            else {
                for entry in entries[node.range] where entry.bounds.intersects(point, latitudeMargin: latitudeMargin, longitudeMargin: longitudeMargin) {
                    result.append(entry.index)
                }
            }
        }
        visit(0)
        // Preserve input order for exact ties in the existing match policies.
        return result.sorted()
    }
}
