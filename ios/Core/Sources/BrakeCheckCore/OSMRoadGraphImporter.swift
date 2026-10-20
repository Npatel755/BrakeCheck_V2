import Foundation

public struct OSMRoadGraphImportResult: Sendable {
    public let graph: OfflineRoadGraph
    public let omittedWayCount: Int
    public let warnings: [String]
}

/// Imports a regional Overpass JSON export containing nodes, road ways, and all
/// restriction relations for those roads. Raw XML/PBF and clipped geometry-only
/// responses are unsupported. Intersections require shared OSM node IDs: crossing
/// lines never acquire invented connections. No service is contacted by this type.
public enum OSMRoadGraphImporter {
    private struct Export: Decodable { let elements: [Element] }
    private struct Element: Decodable {
        let type: String
        let id: Int64
        let lat: Double?
        let lon: Double?
        let nodes: [Int64]?
        let tags: [String: String]?
        let members: [Member]?
    }
    private struct Member: Decodable { let type: String; let ref: Int64; let role: String }
    private struct PreparedWay { let id: Int64; let nodes: [Int64]; let tags: [String: String]; let direction: Int }
    private static let roads: Set<String> = ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service"]
    private static let allowedAccess: Set<String> = ["yes", "permissive", "designated"]

    public static func decode(_ data: Data, name: String = "Imported OpenStreetMap roads", importedAt: Date = Date()) throws -> OfflineRoadGraph {
        try importData(data, name: name, importedAt: importedAt).graph
    }
    public static func importData(_ data: Data, name: String = "Imported OpenStreetMap roads",
                                  importedAt: Date = Date()) throws -> OSMRoadGraphImportResult {
        guard !data.isEmpty, data.count <= 64_000_000 else { throw OfflineNavigationError.invalidGraph }
        let elements = try JSONDecoder().decode(Export.self, from: data).elements
        guard elements.count <= 750_000 else { throw OfflineNavigationError.invalidGraph }
        var coordinates: [Int64: NavigationCoordinate] = [:]
        var blockedNodes: Set<Int64> = []
        var seenElements: Set<String> = []
        for element in elements {
            guard seenElements.insert("\(element.type):\(element.id)").inserted else { throw OfflineNavigationError.invalidGraph }
            guard element.type == "node" else { continue }
            guard let latitude = element.lat, let longitude = element.lon else { throw OfflineNavigationError.invalidGraph }
            let coordinate = NavigationCoordinate(latitude: latitude, longitude: longitude)
            guard coordinate.isValid else { throw OfflineNavigationError.invalidGraph }
            coordinates[element.id] = coordinate
            let tags = normalized(element.tags)
            if let barrier = tags["barrier"], barrier != "no", !explicitAccessAllowed(tags) { blockedNodes.insert(element.id) }
            if hasConditionalDrivingRule(tags) || !accessAllowed(tags) { blockedNodes.insert(element.id) }
        }
        let relations = elements.filter { $0.type == "relation" && normalized($0.tags)["type"] == "restriction" }
        var blockedWays: Set<Int64> = []
        var usableRelations: [(Element, String)] = []
        let supported: Set<String> = ["no_left_turn", "no_right_turn", "no_straight_on", "no_u_turn", "only_left_turn",
            "only_right_turn", "only_straight_on", "only_u_turn", "no_entry", "no_exit"]
        for relation in relations {
            let tags = normalized(relation.tags)
            let exceptions = Set((tags["except"] ?? "").split(separator: ";").map { $0.trimmingCharacters(in: .whitespaces) })
            if !exceptions.isDisjoint(with: ["motorcar", "motor_vehicle", "vehicle"]) { continue }
            let restriction = tags["restriction:motorcar"] ?? tags["restriction:motor_vehicle"]
                ?? tags["restriction:vehicle"] ?? tags["restriction"]
            let members = relation.members ?? []
            let from = members.filter { $0.role == "from" && $0.type == "way" }
            let to = members.filter { $0.role == "to" && $0.type == "way" }
            let via = members.filter { $0.role == "via" }
            guard let restriction, supported.contains(restriction), !tags.keys.contains(where: { $0.contains("conditional") }),
                  !from.isEmpty, !to.isEmpty, via.count == 1, via[0].type == "node", coordinates[via[0].ref] != nil,
                  members.allSatisfy({ ["from", "to", "via"].contains($0.role) }) else {
                // An unsupported restriction is never silently discarded while
                // its roads remain routable.
                blockedWays.formUnion(members.filter { $0.type == "way" }.map(\.ref))
                continue
            }
            usableRelations.append((relation, restriction))
        }
        var prepared: [PreparedWay] = []
        var omitted = 0
        for element in elements where element.type == "way" {
            let tags = normalized(element.tags)
            guard tags["highway"] != nil else { continue }
            let nodes = element.nodes ?? []
            guard let highway = tags["highway"], roads.contains(highway), !blockedWays.contains(element.id),
                  nodes.count >= 2, nodes.count <= 100_000, nodes.allSatisfy({ coordinates[$0] != nil && !blockedNodes.contains($0) }),
                  !hasConditionalDrivingRule(tags), accessAllowed(tags), tags["area"] != "yes",
                  tags["construction"] == nil, let direction = direction(tags),
                  !tags.keys.contains(where: { $0.hasPrefix("restriction:") }) else { omitted += 1; continue }
            prepared.append(PreparedWay(id: element.id, nodes: nodes, tags: tags, direction: direction))
        }
        var edges: [OfflineRoadEdge] = []
        var edgeWays: [String: Int64] = [:]
        var usedNodes: Set<Int64> = []
        for way in prepared {
            let highway = way.tags["highway"]!
            let name = way.tags["name"] ?? way.tags["ref"] ?? "Unnamed road"
            let toll = ["yes", "true", "1"].contains(way.tags["toll"] ?? "no")
            for index in 0..<(way.nodes.count - 1) {
                let a = way.nodes[index], b = way.nodes[index + 1]
                guard a != b, coordinates[a]!.distance(to: coordinates[b]!) > 0.1 else { continue }
                for forward in [true, false] {
                    guard (forward ? way.direction >= 0 : way.direction <= 0),
                          directionalAccessAllowed(way.tags, forward: forward) else { continue }
                    let from = forward ? a : b, to = forward ? b : a
                    let edgeID = "way:\(way.id):\(index):\(forward ? "f" : "r")"
                    edges.append(OfflineRoadEdge(id: edgeID, from: "node:\(from)", to: "node:\(to)", name: name,
                        coordinates: [coordinates[from]!, coordinates[to]!],
                        speedMetersPerSecond: speed(way.tags, highway: highway, forward: forward),
                        isToll: toll, isMotorway: highway == "motorway" || highway == "motorway_link"))
                    edgeWays[edgeID] = way.id; usedNodes.insert(a); usedNodes.insert(b)
                }
            }
        }
        var restrictions: [OfflineTurnRestriction] = []
        let incomingByNode = Dictionary(grouping: edges, by: \.to)
        let outgoingByNode = Dictionary(grouping: edges, by: \.from)
        var unrepresentedWays: Set<Int64> = []
        for (relation, restriction) in usableRelations {
            let members = relation.members!
            let fromWays = Set(members.filter { $0.role == "from" }.map(\.ref))
            let toWays = Set(members.filter { $0.role == "to" }.map(\.ref))
            let via = "node:\(members.first { $0.role == "via" }!.ref)"
            let incoming = (incomingByNode[via] ?? []).filter { fromWays.contains(edgeWays[$0.id]!) }
            let outgoing = (outgoingByNode[via] ?? []).filter { toWays.contains(edgeWays[$0.id]!) }
            guard !incoming.isEmpty, !outgoing.isEmpty else {
                unrepresentedWays.formUnion(fromWays); unrepresentedWays.formUnion(toWays); continue
            }
            for from in incoming {
                var targets = outgoing
                if fromWays == toWays {
                    if restriction.hasSuffix("u_turn") { targets = targets.filter { $0.to == from.from } }
                    else { targets = targets.filter { $0.to != from.from } }
                }
                guard !targets.isEmpty else { unrepresentedWays.formUnion(fromWays); continue }
                restrictions += targets.map { OfflineTurnRestriction(fromEdgeID: from.id, toEdgeID: $0.id,
                    viaNodeID: via, kind: restriction.hasPrefix("only_") ? .only : .prohibited) }
            }
        }
        if !unrepresentedWays.isEmpty {
            // Removing a road that is an only-turn target must not erase the
            // restriction and allow its incoming road to take arbitrary exits.
            let onlyRules = Dictionary(grouping: restrictions.filter { $0.kind == .only }, by: \.fromEdgeID)
            while true {
                edges.removeAll { unrepresentedWays.contains(edgeWays[$0.id]!) }
                let retained = Set(edges.map(\.id))
                let further = Set(onlyRules.compactMap { incoming, rules -> Int64? in
                    retained.contains(incoming) && !rules.contains(where: { retained.contains($0.toEdgeID) }) ? edgeWays[incoming] : nil
                }).subtracting(unrepresentedWays)
                guard !further.isEmpty else { break }
                unrepresentedWays.formUnion(further)
            }
            let retained = Set(edges.map(\.id))
            restrictions.removeAll { !retained.contains($0.fromEdgeID) || !retained.contains($0.toEdgeID) }
            omitted += unrepresentedWays.count
            usedNodes = Set(edges.flatMap { [$0.from, $0.to] }.compactMap { Int64($0.dropFirst(5)) })
        }
        let nodes = usedNodes.sorted().map { OfflineRoadNode(id: "node:\($0)", coordinate: coordinates[$0]!) }
        let graph = OfflineRoadGraph(name: name, attribution: "© OpenStreetMap contributors · ODbL 1.0",
            createdAt: importedAt, nodes: nodes, edges: edges, turnRestrictions: restrictions)
        try graph.validate()
        var warnings = ["Routing depends on a complete regional export including all driving restriction relations.",
                        "Offline travel times use road-class and tagged speed estimates; live traffic is unavailable."]
        if omitted > 0 { warnings.append("Omitted \(omitted) roads with unsupported, incomplete, or restricted driving data.") }
        return OSMRoadGraphImportResult(graph: graph, omittedWayCount: omitted, warnings: warnings)
    }
    private static func normalized(_ tags: [String: String]?) -> [String: String] {
        // Preserve display labels while normalizing routing values.
        (tags ?? [:]).mapValues { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .reduce(into: [:]) { result, pair in result[pair.key] = ["name", "ref"].contains(pair.key) ? pair.value : pair.value.lowercased() }
    }
    private static func accessAllowed(_ tags: [String: String]) -> Bool {
        guard let value = tags["motorcar"] ?? tags["motor_vehicle"] ?? tags["vehicle"] ?? tags["access"] else { return true }
        return allowedAccess.contains(value)
    }
    private static func explicitAccessAllowed(_ tags: [String: String]) -> Bool {
        guard let value = tags["motorcar"] ?? tags["motor_vehicle"] ?? tags["vehicle"] ?? tags["access"] else { return false }
        return allowedAccess.contains(value)
    }
    private static func directionalAccessAllowed(_ tags: [String: String], forward: Bool) -> Bool {
        let suffix = forward ? ":forward" : ":backward"
        guard let value = tags["motorcar" + suffix] ?? tags["motor_vehicle" + suffix]
            ?? tags["vehicle" + suffix] ?? tags["access" + suffix] else { return true }
        return allowedAccess.contains(value)
    }
    private static func hasConditionalDrivingRule(_ tags: [String: String]) -> Bool {
        tags.keys.contains { key in
            key.contains("conditional") && ["access", "vehicle", "motor_vehicle", "motorcar", "oneway", "maxspeed", "toll"]
                .contains(where: { key == $0 + ":conditional" || key.hasPrefix($0 + ":") })
        }
    }
    private static func direction(_ tags: [String: String]) -> Int? {
        let value = tags["oneway:motorcar"] ?? tags["oneway:motor_vehicle"] ?? tags["oneway"]
        switch value {
        case "yes", "true", "1": return 1
        case "-1": return -1
        case "no", "false", "0": return 0
        case nil: return tags["junction"] == "roundabout" || tags["highway"] == "motorway" ? 1 : 0
        default: return nil
        }
    }
    private static func speed(_ tags: [String: String], highway: String, forward: Bool) -> Double {
        let raw = tags[forward ? "maxspeed:forward" : "maxspeed:backward"] ?? tags["maxspeed"]
        if let raw {
            let isMPH = raw.hasSuffix("mph")
            let numeric = raw.replacingOccurrences(of: "mph", with: "").replacingOccurrences(of: "km/h", with: "")
                .replacingOccurrences(of: "kph", with: "").trimmingCharacters(in: .whitespaces)
            if let value = Double(numeric), value.isFinite, value > 0 {
                // Speeds are planning estimates, not a speed-limit display.
                return min(65, max(0.5, value * (isMPH ? 0.44704 : 1 / 3.6) * 0.8))
            }
        }
        switch highway {
        case "motorway": return 25
        case "trunk", "primary": return 18
        case "secondary", "tertiary": return 13.89
        case "living_street", "service": return 4.17
        default: return 8.33
        }
    }
}
