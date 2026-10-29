import Foundation

public struct RecentDestination: Codable, Identifiable {
    public let id: UUID
    public let title: String
    public let subtitle: String
    public let latitude: Double
    public let longitude: Double
    public let selectedAt: Date
    public init(id: UUID = UUID(), title: String, subtitle: String, latitude: Double,
                longitude: Double, selectedAt: Date = Date()) {
        self.id = id; self.title = title; self.subtitle = subtitle
        self.latitude = latitude; self.longitude = longitude; self.selectedAt = selectedAt
    }
    public var valid: Bool {
        !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && latitude.isFinite && longitude.isFinite && (-90...90).contains(latitude)
            && (-180...180).contains(longitude) && selectedAt.timeIntervalSince1970.isFinite
    }
}

public enum RecentDestinations {
    public static func normalized(_ items: [RecentDestination]) -> [RecentDestination] {
        var result: [RecentDestination] = []
        for item in items.filter(\.valid).sorted(by: { $0.selectedAt > $1.selectedAt }) {
            guard !result.contains(where: {
                $0.id == item.id || (abs($0.latitude - item.latitude) < 0.0001
                    && abs($0.longitude - item.longitude) < 0.0001)
            }) else { continue }
            result.append(item)
            if result.count == 20 { break }
        }
        return result
    }
    public static func adding(_ item: RecentDestination, to items: [RecentDestination]) -> [RecentDestination] {
        guard item.valid else { return normalized(items) }
        return normalized([item] + items.filter {
            $0.id != item.id && !(abs($0.latitude - item.latitude) < 0.0001
                && abs($0.longitude - item.longitude) < 0.0001)
        })
    }
}
