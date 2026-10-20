import Foundation

public struct OfflineRoadNode: Codable, Equatable, Sendable {
    public var id: String
    public var coordinate: NavigationCoordinate
    public init(id: String, coordinate: NavigationCoordinate) { self.id = id; self.coordinate = coordinate }
}

/// Every edge is directed. Importers must emit a reverse edge only where driving
/// in that direction is legal. Geometry is ordered from `from` to `to`.
public struct OfflineRoadEdge: Codable, Equatable, Sendable {
    public var id: String
    public var from: String
    public var to: String
    public var name: String
    public var coordinates: [NavigationCoordinate]
    public var speedMetersPerSecond: Double
    public var allowsDriving: Bool
    public var isToll: Bool
    public var isMotorway: Bool
    public init(id: String, from: String, to: String, name: String = "Road",
                coordinates: [NavigationCoordinate], speedMetersPerSecond: Double = 13.89,
                allowsDriving: Bool = true, isToll: Bool = false, isMotorway: Bool = false) {
        self.id = id; self.from = from; self.to = to; self.name = name; self.coordinates = coordinates
        self.speedMetersPerSecond = speedMetersPerSecond; self.allowsDriving = allowsDriving
        self.isToll = isToll; self.isMotorway = isMotorway
    }
    public var distance: Double {
        zip(coordinates, coordinates.dropFirst()).reduce(0) { $0 + $1.0.distance(to: $1.1) }
    }
}

public struct OfflineTurnRestriction: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable { case prohibited, only }
    public var fromEdgeID: String
    public var toEdgeID: String
    public var viaNodeID: String
    public var kind: Kind
    public init(fromEdgeID: String, toEdgeID: String, viaNodeID: String, kind: Kind = .prohibited) {
        self.fromEdgeID = fromEdgeID; self.toEdgeID = toEdgeID; self.viaNodeID = viaNodeID; self.kind = kind
    }
}

/// A bounded, preprocessed driving graph, not raw OSM XML. Restriction/access
/// processing belongs to its producer; the router does not invent connectivity.
public struct OfflineRoadGraph: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    public var name: String
    public var attribution: String
    public var createdAt: Date
    public var nodes: [OfflineRoadNode]
    public var edges: [OfflineRoadEdge]
    public var turnRestrictions: [OfflineTurnRestriction]
    public init(name: String, attribution: String, createdAt: Date = Date(), nodes: [OfflineRoadNode],
                edges: [OfflineRoadEdge], turnRestrictions: [OfflineTurnRestriction] = []) {
        schemaVersion = 1; self.name = name; self.attribution = attribution; self.createdAt = createdAt
        self.nodes = nodes; self.edges = edges; self.turnRestrictions = turnRestrictions
    }
    public static func decode(_ data: Data) throws -> OfflineRoadGraph {
        guard !data.isEmpty, data.count <= 64_000_000 else { throw OfflineNavigationError.invalidGraph }
        let graph = try JSONDecoder().decode(Self.self, from: data)
        try graph.validate()
        return graph
    }
    public func validate() throws {
        guard schemaVersion == 1, !name.isEmpty, !attribution.isEmpty,
              createdAt.timeIntervalSince1970.isFinite, !nodes.isEmpty, nodes.count <= 250_000,
              !edges.isEmpty, edges.count <= 500_000, turnRestrictions.count <= 500_000,
              Set(nodes.map(\.id)).count == nodes.count, Set(edges.map(\.id)).count == edges.count,
              nodes.allSatisfy({ !$0.id.isEmpty && $0.coordinate.isValid }) else {
            throw OfflineNavigationError.invalidGraph
        }
        let nodeMap = Dictionary(uniqueKeysWithValues: nodes.map { ($0.id, $0.coordinate) })
        var coordinateCount = 0
        for edge in edges {
            coordinateCount += edge.coordinates.count
            guard !edge.id.isEmpty, let from = nodeMap[edge.from], let to = nodeMap[edge.to],
                  edge.coordinates.count >= 2, coordinateCount <= 2_000_000,
                  edge.coordinates.allSatisfy(\.isValid),
                  edge.coordinates.first!.distance(to: from) <= 1,
                  edge.coordinates.last!.distance(to: to) <= 1,
                  edge.speedMetersPerSecond.isFinite, (0.5...65).contains(edge.speedMetersPerSecond),
                  edge.distance.isFinite, edge.distance > 0.1, edge.distance <= 100_000 else {
                throw OfflineNavigationError.invalidGraph
            }
        }
        let edgeMap = Dictionary(uniqueKeysWithValues: edges.map { ($0.id, $0) })
        for rule in turnRestrictions {
            guard let from = edgeMap[rule.fromEdgeID], let to = edgeMap[rule.toEdgeID],
                  from.to == rule.viaNodeID, to.from == rule.viaNodeID else {
                throw OfflineNavigationError.invalidGraph
            }
        }
    }
}

public enum OfflineNavigationError: Error, Equatable, LocalizedError {
    case invalidGraph, invalidCoordinate, outsideCoverage, noNearbyDrivingRoad, ambiguousRoadPosition, noLegalRoute, routeTooLong
    public var errorDescription: String? {
        switch self {
        case .invalidGraph: return "The offline road graph is missing, damaged, or unsupported."
        case .invalidCoordinate: return "A route endpoint is invalid."
        case .outsideCoverage: return "This destination is outside the downloaded road region."
        case .noNearbyDrivingRoad: return "No compatible driving road is close enough to the location."
        case .ambiguousRoadPosition: return "The location is between distinct roads. Choose a more precise road position."
        case .noLegalRoute: return "The downloaded graph has no connected legal driving route."
        case .routeTooLong: return "The route exceeds the offline routing limit."
        }
    }
}

public struct OfflineRoadSnap: Equatable, Sendable {
    public let edgeID: String
    public let coordinate: NavigationCoordinate
    public let bearing: Double
    public let distanceFromRoad: Double
    public let confidence: Double
}

public struct OfflineNavigationRouter {
    private struct Projection {
        let edge: Int
        let segment: Int
        let fraction: Double
        let along: Double
        let coordinate: NavigationCoordinate
        let distance: Double
        let bearing: Double
    }
    private struct QueueEntry { let edge: Int; let cost: Double }
    private struct MinHeap {
        var values: [QueueEntry] = []
        mutating func push(_ value: QueueEntry) {
            values.append(value); var index = values.count - 1
            while index > 0 {
                let parent = (index - 1) / 2
                guard values[index].cost < values[parent].cost else { break }
                values.swapAt(index, parent); index = parent
            }
        }
        mutating func pop() -> QueueEntry? {
            guard !values.isEmpty else { return nil }
            if values.count == 1 { return values.removeLast() }
            let result = values[0]; values[0] = values.removeLast(); var index = 0
            while index * 2 + 1 < values.count {
                var child = index * 2 + 1
                if child + 1 < values.count, values[child + 1].cost < values[child].cost { child += 1 }
                guard values[child].cost < values[index].cost else { break }
                values.swapAt(child, index); index = child
            }
            return result
        }
    }
    public let graph: OfflineRoadGraph
    private let outgoing: [String: [Int]]
    private let restrictions: [String: [OfflineTurnRestriction]]
    private let lengths: [Double]
    private let edgeIndex: [String: Int]
    private let minLatitude: Double
    private let maxLatitude: Double
    private let minLongitude: Double
    private let maxLongitude: Double

    public init(graph: OfflineRoadGraph) throws {
        try graph.validate(); self.graph = graph
        outgoing = Dictionary(grouping: graph.edges.indices, by: { graph.edges[$0].from })
        restrictions = Dictionary(grouping: graph.turnRestrictions, by: \.fromEdgeID)
        lengths = graph.edges.map(\.distance)
        edgeIndex = Dictionary(uniqueKeysWithValues: graph.edges.enumerated().map { ($0.element.id, $0.offset) })
        minLatitude = graph.nodes.map(\.coordinate.latitude).min()!
        maxLatitude = graph.nodes.map(\.coordinate.latitude).max()!
        minLongitude = graph.nodes.map(\.coordinate.longitude).min()!
        maxLongitude = graph.nodes.map(\.coordinate.longitude).max()!
    }

    public func snap(_ coordinate: NavigationCoordinate, bearing: Double? = nil,
                     preferences: NavigationRoutePreferences = .init(), maximumDistance: Double = 35) throws -> OfflineRoadSnap {
        let candidates = try projections(coordinate, bearing: bearing, preferences: preferences, maximumDistance: maximumDistance)
        let first = candidates[0]
        let ambiguous = candidates.dropFirst().contains { other in
            other.coordinate.distance(to: first.coordinate) > 2 && other.distance <= first.distance + 3
        }
        return OfflineRoadSnap(edgeID: graph.edges[first.edge].id, coordinate: first.coordinate, bearing: first.bearing,
                               distanceFromRoad: first.distance,
                               confidence: max(0, 1 - first.distance / maximumDistance - (ambiguous ? 0.35 : 0)))
    }

    public func route(from start: NavigationCoordinate, to destination: NavigationCoordinate,
                      preferences: NavigationRoutePreferences = .init(), initialBearing: Double? = nil,
                      title: String = "Offline route") throws -> SavedNavigationRoute {
        try calculateRoute(from: start, to: destination, preferences: preferences,
            initialBearing: initialBearing, title: title, requiredStartEdge: nil).route
    }

    private func calculateRoute(from start: NavigationCoordinate, to destination: NavigationCoordinate,
                      preferences: NavigationRoutePreferences, initialBearing: Double?,
                      title: String, requiredStartEdge: Int?) throws -> (route: SavedNavigationRoute, endEdge: Int) {
        let starts = try projections(start, bearing: initialBearing, preferences: preferences, maximumDistance: 35)
            .filter { requiredStartEdge == nil || $0.edge == requiredStartEdge }
        guard !starts.isEmpty else { throw OfflineNavigationError.noLegalRoute }
        let ends = try projections(destination, bearing: nil, preferences: preferences, maximumDistance: 35)
        // Projection candidates already account for a valid initial bearing.
        // A heading cannot distinguish two parallel roads in the same direction,
        // and an invalid bearing must never bypass the ambiguity check.
        if requiredStartEdge == nil && ambiguousParallelRoads(starts)
            || ambiguousParallelRoads(ends) { throw OfflineNavigationError.ambiguousRoadPosition }
        guard start.distance(to: destination) <= 500_000 else { throw OfflineNavigationError.routeTooLong }
        var best: (cost: Double, start: Projection, end: Projection, path: [Int])?
        for start in starts {
            // A direct same-edge route never turns around on a one-way road.
            for end in ends where start.edge == end.edge && end.along > start.along + 0.1 {
                let cost = (end.along - start.along) / graph.edges[start.edge].speedMetersPerSecond
                if best == nil || cost < best!.cost { best = (cost, start, end, []) }
            }
            let initialCost = (lengths[start.edge] - start.along) / graph.edges[start.edge].speedMetersPerSecond
            var costs = [start.edge: initialCost]
            var parent: [Int: Int] = [:]
            var queue = MinHeap(); queue.push(QueueEntry(edge: start.edge, cost: initialCost))
            var visited = 0
            while let current = queue.pop() {
                guard current.cost <= (costs[current.edge] ?? .infinity) else { continue }
                if current.cost > (best?.cost ?? .infinity) { continue }
                visited += 1
                guard visited <= 500_000 else { throw OfflineNavigationError.routeTooLong }
                let incoming = graph.edges[current.edge]
                for end in ends where graph.edges[end.edge].from == incoming.to && canTurn(from: current.edge, to: end.edge) {
                    let total = current.cost + end.along / graph.edges[end.edge].speedMetersPerSecond
                    if best == nil || total < best!.cost {
                        var path = [current.edge]
                        while let previous = parent[path.last!] { path.append(previous) }
                        best = (total, start, end, Array(path.reversed()))
                    }
                }
                for next in outgoing[incoming.to] ?? [] where permitted(graph.edges[next], preferences: preferences)
                    && canTurn(from: current.edge, to: next) {
                    let cost = current.cost + lengths[next] / graph.edges[next].speedMetersPerSecond
                    if cost < (costs[next] ?? .infinity) {
                        costs[next] = cost; parent[next] = current.edge
                        queue.push(QueueEntry(edge: next, cost: cost))
                    }
                }
            }
        }
        guard let best else { throw OfflineNavigationError.noLegalRoute }
        var pieces: [(Int, [NavigationCoordinate])] = []
        if best.path.isEmpty {
            let edge = graph.edges[best.start.edge]
            var points = [best.start.coordinate]
            if best.start.segment < best.end.segment {
                points += Array(edge.coordinates[(best.start.segment + 1)...best.end.segment])
            }
            points.append(best.end.coordinate); pieces = [(best.start.edge, points)]
        } else {
            let edge = graph.edges[best.start.edge]
            pieces.append((best.start.edge, [best.start.coordinate] + Array(edge.coordinates.dropFirst(best.start.segment + 1))))
            for index in best.path.dropFirst() { pieces.append((index, graph.edges[index].coordinates)) }
            let endEdge = graph.edges[best.end.edge]
            pieces.append((best.end.edge, Array(endEdge.coordinates.prefix(best.end.segment + 1)) + [best.end.coordinate]))
        }
        var coordinates: [NavigationCoordinate] = []
        var steps: [NavigationRouteStep] = []
        var previousEdgeIndex: Int?
        for (index, points) in pieces {
            let length = zip(points, points.dropFirst()).reduce(0) { $0 + $1.0.distance(to: $1.1) }
            guard length > 0.1 else { continue }
            let edge = graph.edges[index]
            let instruction: String
            if let previous = steps.last?.coordinates, previous.count >= 2, points.count >= 2 {
                let before = Self.bearing(previous[previous.count - 2], previous.last!)
                let after = Self.bearing(points[0], points[1])
                let turn = (after - before + 540).truncatingRemainder(dividingBy: 360) - 180
                instruction = abs(turn) >= 150 ? "Make a U-turn onto \(edge.name)"
                    : abs(turn) >= 30 ? "Turn \(turn > 0 ? "right" : "left") onto \(edge.name)"
                    : "Continue on \(edge.name)"
            } else { instruction = "Head along \(edge.name)" }
            let previousEdge = previousEdgeIndex.map { graph.edges[$0] }
            let isSameRoadContinuation = instruction.hasPrefix("Continue on ")
                && previousEdge?.name == edge.name && previousEdge?.isToll == edge.isToll
                && previousEdge?.allowsDriving == edge.allowsDriving && previousEdge?.isMotorway == edge.isMotorway
            if isSameRoadContinuation, !steps.isEmpty {
                // OSM shape nodes split directed graph edges without creating
                // maneuvers. Preserve their geometry and pace while speaking a
                // single instruction for the continuous road.
                let last = steps.count - 1
                steps[last].distance += length
                steps[last].expectedTravelTime += length / edge.speedMetersPerSecond
                for coordinate in points where steps[last].coordinates.last.map({ $0.distance(to: coordinate) > 0.01 }) ?? true {
                    steps[last].coordinates.append(coordinate)
                }
            } else {
                steps.append(NavigationRouteStep(instruction: instruction, distance: length,
                    expectedTravelTime: length / edge.speedMetersPerSecond, coordinates: points,
                    notice: edge.isToll ? "Toll road" : nil))
            }
            previousEdgeIndex = index
            for coordinate in points where coordinates.last.map({ $0.distance(to: coordinate) > 0.01 }) ?? true {
                coordinates.append(coordinate)
            }
        }
        guard coordinates.count >= 2, !steps.isEmpty else { throw OfflineNavigationError.noLegalRoute }
        let route = SavedNavigationRoute(title: title, coordinates: coordinates, steps: steps,
            distance: steps.reduce(0) { $0 + $1.distance }, expectedTravelTime: steps.reduce(0) { $0 + $1.expectedTravelTime },
            source: "Offline · \(graph.name) · \(graph.attribution)", preferences: preferences)
        guard route.isValid else { throw OfflineNavigationError.noLegalRoute }
        let terminalEdge = best.end.along > 0.1 ? best.end.edge : (best.path.last ?? best.start.edge)
        return (route, terminalEdge)
    }

    /// Visits stops in the requested order. Each leg is calculated separately so
    /// an unreachable stop fails the entire plan rather than being silently skipped.
    public func route(from start: NavigationCoordinate, stops: [NavigationRouteStop],
                      preferences: NavigationRoutePreferences = .init(), initialBearing: Double? = nil,
                      title: String = "Offline route") throws -> SavedNavigationRoute {
        guard !stops.isEmpty, stops.count <= 20, stops.allSatisfy(\.isValid) else {
            throw OfflineNavigationError.invalidCoordinate
        }
        var current = start
        var incomingEdge: Int?
        var coordinates: [NavigationCoordinate] = []
        var steps: [NavigationRouteStep] = []
        var routedStops: [NavigationRouteStop] = []
        var cumulativeDistance = 0.0
        for (index, stop) in stops.enumerated() {
            let result = try calculateRoute(from: current, to: stop.coordinate, preferences: preferences,
                initialBearing: index == 0 ? initialBearing : nil, title: title, requiredStartEdge: incomingEdge)
            let leg = result.route
            if let previous = coordinates.last, let first = leg.coordinates.first, previous.distance(to: first) > 1 {
                // Never draw a fictitious connector across carriageways between legs.
                throw OfflineNavigationError.noLegalRoute
            }
            coordinates += coordinates.isEmpty ? leg.coordinates : Array(leg.coordinates.dropFirst())
            cumulativeDistance += leg.distance
            let routedEndpoint = leg.coordinates.last!
            routedStops.append(NavigationRouteStop(id: stop.id, title: stop.title, coordinate: routedEndpoint,
                dwellTime: stop.dwellTime, routeDistanceFromStart: cumulativeDistance))
            steps += leg.steps; current = routedEndpoint; incomingEdge = result.endEdge
        }
        return SavedNavigationRoute(title: title, coordinates: coordinates, steps: steps,
            distance: steps.reduce(0) { $0 + $1.distance },
            expectedTravelTime: steps.reduce(0) { $0 + $1.expectedTravelTime } + stops.reduce(0) { $0 + $1.dwellTime },
            source: "Offline · \(graph.name) · \(graph.attribution)", stops: routedStops, preferences: preferences)
    }

    private func canTurn(from incoming: Int, to outgoing: Int) -> Bool {
        let rules = restrictions[graph.edges[incoming].id] ?? []
        let outgoingID = graph.edges[outgoing].id
        if rules.contains(where: { $0.kind == .prohibited && $0.toEdgeID == outgoingID }) { return false }
        let only = rules.filter { $0.kind == .only }
        return only.isEmpty || only.contains { $0.toEdgeID == outgoingID }
    }
    private func permitted(_ edge: OfflineRoadEdge, preferences: NavigationRoutePreferences) -> Bool {
        edge.allowsDriving && !(preferences.avoidTolls && edge.isToll)
            && !(preferences.avoidMotorways && edge.isMotorway)
    }
    private func ambiguousParallelRoads(_ candidates: [Projection]) -> Bool {
        guard let nearest = candidates.first else { return false }
        return candidates.dropFirst().contains {
            let difference = abs(($0.bearing - nearest.bearing + 540).truncatingRemainder(dividingBy: 360) - 180)
            return $0.coordinate.distance(to: nearest.coordinate) > 2
                && $0.distance < nearest.distance + 2 && (difference < 20 || difference > 160)
        }
    }
    private func projections(_ coordinate: NavigationCoordinate, bearing: Double?,
                             preferences: NavigationRoutePreferences, maximumDistance: Double) throws -> [Projection] {
        guard coordinate.isValid, maximumDistance.isFinite, (1...100).contains(maximumDistance) else {
            throw OfflineNavigationError.invalidCoordinate
        }
        // Bounding-box rejection prevents a downloaded region from pretending to
        // cover arbitrary coordinates. A narrow edge-distance check follows.
        let margin = maximumDistance / 111_000
        let longitudeMargin = margin / max(0.01, cos(coordinate.latitude * .pi / 180))
        guard coordinate.latitude >= minLatitude - margin, coordinate.latitude <= maxLatitude + margin,
              coordinate.longitude >= minLongitude - longitudeMargin,
              coordinate.longitude <= maxLongitude + longitudeMargin else { throw OfflineNavigationError.outsideCoverage }
        let heading = bearing.flatMap { $0.isFinite && (0..<360).contains($0) ? $0 : nil }
        var candidates: [Projection] = []
        for (index, edge) in graph.edges.enumerated() where permitted(edge, preferences: preferences) {
            var along = 0.0
            var closest: Projection?
            for segment in 0..<(edge.coordinates.count - 1) {
                let a = edge.coordinates[segment], b = edge.coordinates[segment + 1]
                let length = a.distance(to: b)
                defer { along += length }
                guard length > 0.01 else { continue }
                let direction = Self.bearing(a, b)
                if let heading, abs((direction - heading + 540).truncatingRemainder(dividingBy: 360) - 180) > 80 { continue }
                let scale = max(1e-9, cos((a.latitude + b.latitude) * .pi / 360))
                let dx = Self.longitudeDelta(a.longitude, b.longitude) * scale
                let dy = b.latitude - a.latitude
                let px = Self.longitudeDelta(a.longitude, coordinate.longitude) * scale
                let py = coordinate.latitude - a.latitude
                let fraction = min(1, max(0, (px * dx + py * dy) / (dx * dx + dy * dy)))
                let snapped = NavigationCoordinate(latitude: a.latitude + dy * fraction,
                    longitude: (a.longitude + dx / scale * fraction + 540).truncatingRemainder(dividingBy: 360) - 180)
                let distance = snapped.distance(to: coordinate)
                if distance <= maximumDistance, closest == nil || distance < closest!.distance {
                    closest = Projection(edge: index, segment: segment, fraction: fraction, along: along + length * fraction,
                        coordinate: snapped, distance: distance, bearing: direction)
                }
            }
            if let closest { candidates.append(closest) }
        }
        candidates.sort { $0.distance == $1.distance ? graph.edges[$0.edge].id < graph.edges[$1.edge].id : $0.distance < $1.distance }
        guard let nearest = candidates.first else { throw OfflineNavigationError.noNearbyDrivingRoad }
        // Do not jump to a distant connected road merely because the nearest road
        // has no path. Heading also removes the opposite carriageway when known.
        let nearestCandidates = candidates.filter { $0.distance <= nearest.distance + 3 }
        if let heading {
            let closestHeading = nearestCandidates.map {
                abs(($0.bearing - heading + 540).truncatingRemainder(dividingBy: 360) - 180)
            }.min() ?? 180
            return Array(nearestCandidates.filter {
                abs(($0.bearing - heading + 540).truncatingRemainder(dividingBy: 360) - 180) <= closestHeading + 20
            }.prefix(8))
        }
        return Array(nearestCandidates.prefix(8))
    }
    private static func longitudeDelta(_ a: Double, _ b: Double) -> Double {
        (b - a + 540).truncatingRemainder(dividingBy: 360) - 180
    }
    private static func bearing(_ a: NavigationCoordinate, _ b: NavigationCoordinate) -> Double {
        let latitude = (a.latitude + b.latitude) * .pi / 360
        return (atan2(longitudeDelta(a.longitude, b.longitude) * cos(latitude), b.latitude - a.latitude)
            * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }
}
