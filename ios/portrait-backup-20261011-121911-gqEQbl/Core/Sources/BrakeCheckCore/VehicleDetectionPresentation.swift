import Foundation

/// Brief visual continuity only. These entries never enter lead selection,
/// motion fitting, range or risk. A held box retains its real capture time.
public struct VehicleDetectionPresentation {
    public struct Entry {
        public let vehicle: LeadVehicle
        public let capturedAt: TimeInterval
        public let isFreshDetection: Bool
    }
    private var recent: [UUID: Entry] = [:]
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { recent = [:]; lastUpdate = nil }
    public mutating func update(_ vehicles: [LeadVehicle], at time: TimeInterval) -> [Entry] {
        guard time.isFinite else { reset(); return [] }
        if let previous = lastUpdate, time <= previous { return [] }
        lastUpdate = time
        recent = recent.filter { time >= $0.value.capturedAt && time - $0.value.capturedAt <= 0.45 + 1e-6 }
        let fresh = Array(vehicles.filter {
            $0.trackID != nil && LeadVehicle.validBox($0.box) && $0.confidence.isFinite && (0.28...1).contains($0.confidence)
        }.prefix(64))
        let ids = Set(fresh.compactMap(\.trackID))
        let held = recent.values.filter { entry in
            guard let id = entry.vehicle.trackID, !ids.contains(id) else { return false }
            // Never draw an older identity over a newly associated object.
            return !fresh.contains { Self.overlap($0.box, entry.vehicle.box) >= 0.15 }
        }.map { Entry(vehicle: $0.vehicle, capturedAt: $0.capturedAt, isFreshDetection: false) }
        let current = fresh.map { Entry(vehicle: $0, capturedAt: time, isFreshDetection: true) }
        for entry in current { if let id = entry.vehicle.trackID { recent[id] = entry } }
        if recent.count > 128 {
            recent = Dictionary(uniqueKeysWithValues: recent.sorted { $0.value.capturedAt > $1.value.capturedAt }.prefix(128).map { ($0.key, $0.value) })
        }
        return current + held.sorted { $0.capturedAt > $1.capturedAt }.prefix(max(0, 64 - current.count))
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
    }
}
