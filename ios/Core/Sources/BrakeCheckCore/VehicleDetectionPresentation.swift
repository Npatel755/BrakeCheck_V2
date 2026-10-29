import Foundation

/// Brief visual continuity and smoothing only. These entries never enter lead
/// selection, motion fitting, range or risk. A held box retains its capture time.
public struct VehicleDetectionPresentation {
    public struct Entry {
        public let vehicle: LeadVehicle
        public let capturedAt: TimeInterval
        public let isFreshDetection: Bool
    }
    private struct Recent {
        var entry: Entry
        let detectedBox: CGRect
    }
    private var recent: [UUID: Recent] = [:]
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { recent = [:]; lastUpdate = nil }
    /// Use the same displayed geometry for a selected lead and its detector box.
    /// This accessor must not be used to estimate distance or motion.
    public func box(for id: UUID) -> CGRect? { recent[id]?.entry.vehicle.box }
    public mutating func update(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                preferredID: UUID? = nil, trackedBox: CGRect? = nil) -> [Entry] {
        guard time.isFinite else { reset(); return [] }
        if let previous = lastUpdate, time <= previous { return [] }
        lastUpdate = time
        recent = recent.filter { time >= $0.value.entry.capturedAt && time - $0.value.entry.capturedAt <= 0.45 + 1e-6 }
        let candidates = vehicles.filter {
            $0.trackID != nil && LeadVehicle.validBox($0.box) && $0.confidence.isFinite && (0.28...1).contains($0.confidence)
        }
        var fresh: [LeadVehicle] = []
        var seen: Set<UUID> = []
        for vehicle in candidates.sorted(by: { $0.confidence > $1.confidence }) {
            guard let id = vehicle.trackID, seen.insert(id).inserted else { continue }
            fresh.append(vehicle)
            if fresh.count == 64 { break }
        }
        let ids = Set(fresh.compactMap(\.trackID))
        // Permanently retire a replaced display identity. Merely hiding it on
        // this frame lets its ghost reappear when the replacement misses next.
        recent = recent.filter { id, record in
            ids.contains(id) || !fresh.contains { Self.overlap($0.box, record.detectedBox) >= 0.15 }
        }
        var held: [Entry] = []
        for (id, record) in recent where !ids.contains(id) {
            var vehicle = record.entry.vehicle
            if id == preferredID, let trackedBox,
               VehicleTrackSupport.supportsVisualBox(trackedBox, relativeTo: record.detectedBox) {
                vehicle = Self.replacingBox(vehicle, with: trackedBox)
            }
            let entry = Entry(vehicle: vehicle, capturedAt: record.entry.capturedAt, isFreshDetection: false)
            recent[id]?.entry = entry
            held.append(entry)
        }
        let current = fresh.map { vehicle -> Entry in
            let id = vehicle.trackID!
            let box = recent[id].map { Self.smoothedBox(vehicle.box, prior: $0, at: time) } ?? vehicle.box
            let entry = Entry(vehicle: Self.replacingBox(vehicle, with: box), capturedAt: time, isFreshDetection: true)
            recent[id] = Recent(entry: entry, detectedBox: vehicle.box)
            return entry
        }
        if recent.count > 128 {
            recent = Dictionary(uniqueKeysWithValues: recent.sorted { $0.value.entry.capturedAt > $1.value.entry.capturedAt }.prefix(128).map { ($0.key, $0.value) })
        }
        return current + held.sorted { $0.capturedAt > $1.capturedAt }.prefix(max(0, 64 - current.count))
    }
    private static func replacingBox(_ vehicle: LeadVehicle, with box: CGRect) -> LeadVehicle {
        LeadVehicle(box: box, label: vehicle.label, confidence: vehicle.confidence,
                    viewpoint: vehicle.viewpoint, trackID: vehicle.trackID, travel: vehicle.travel)
    }
    private static func smoothedBox(_ box: CGRect, prior: Recent, at time: TimeInterval) -> CGRect {
        let age = time - prior.entry.capturedAt
        let raw = prior.detectedBox
        let width = Double(box.width / raw.width), height = Double(box.height / raw.height)
        guard age <= 0.30, (0.75...1.33).contains(width), (0.75...1.33).contains(height),
              overlap(box, raw) >= 0.45 else { return box }
        // Light smoothing reduces one-frame detector jitter. Increase response
        // for real motion/scale changes and snap after a gap or changed crop.
        let movement = max(abs(Double(box.midX - raw.midX)) / Double(raw.width),
                           abs(Double(box.midY - raw.midY)) / Double(raw.height))
        let scale = max(abs(width - 1), abs(height - 1))
        let response = min(1, max(movement / 0.20, scale / 0.18))
        let base = 1 - exp(-age / 0.14)
        let blend = max(0.30, base + (1 - base) * response * 0.60)
        let old = prior.entry.vehicle.box
        let result = CGRect(x: old.minX + (box.minX - old.minX) * blend,
                            y: old.minY + (box.minY - old.minY) * blend,
                            width: old.width + (box.width - old.width) * blend,
                            height: old.height + (box.height - old.height) * blend)
        return LeadVehicle.validBox(result) ? result : box
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
    }
}
