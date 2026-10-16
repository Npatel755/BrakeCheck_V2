import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// A square model input with a letterboxed crop. All detector coordinates
/// are mapped back to the original, upright camera before association/range.
public struct VehicleSearchCrop {
    public let region: CGRect
    public let content: CGRect
    public init?(region: CGRect, imageAspect: Double) {
        guard imageAspect.isFinite, (0.1...10).contains(imageAspect),
              LeadVehicle.validBox(region) else { return nil }
        self.region = region
        let aspect = Double(region.width / region.height) * imageAspect
        let width = min(1, aspect), height = min(1, 1 / aspect)
        content = CGRect(x: (1 - width) / 2, y: (1 - height) / 2, width: width, height: height)
    }
    public func fullImageBox(from box: CGRect) -> CGRect? {
        guard LeadVehicle.validBox(box),
              // Crop-truncated silhouettes cannot supply full-car range.
              box.minX > content.minX + content.width * 0.01,
              box.maxX < content.maxX - content.width * 0.01,
              box.minY > content.minY + content.height * 0.01,
              box.maxY < content.maxY - content.height * 0.01 else { return nil }
        let full = CGRect(x: region.minX + (box.minX - content.minX) / content.width * region.width,
                          y: region.minY + (box.minY - content.minY) / content.height * region.height,
                          width: box.width / content.width * region.width,
                          height: box.height / content.height * region.height)
        return VehicleDetectionFilter.normalizedBox(full)
    }
}

/// One conditional recovery inference, never a second full-rate detector.
/// Strong whole-frame boxes win; a missed target can be searched near its
/// independently tracked position. Unconfirmed recovery boxes need the same
/// ID, rear, path and motion checks as every other detector measurement.
public struct VehicleDetectionRecovery {
    private var lastAttempt: TimeInterval = -.infinity
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { lastAttempt = -.infinity; lastUpdate = nil }

    public mutating func propose(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                 preferredBox: CGRect?, lastDetectedAt: TimeInterval?,
                                 trackedBox: CGRect?, lane: LanePath?, imageAspect: Double,
                                 interval: TimeInterval?, captureAge: TimeInterval,
                                 vehicleLatency: TimeInterval) -> VehicleSearchCrop? {
        guard time.isFinite else { reset(); return nil }
        if let previous = lastUpdate, time <= previous { return nil }
        lastUpdate = time
        guard let interval, interval.isFinite, interval >= 0.75,
              time - lastAttempt >= interval - 1e-6,
              captureAge.isFinite, (0...0.25).contains(captureAge),
              vehicleLatency.isFinite, (0...0.20).contains(vehicleLatency),
              captureAge + max(0.06, vehicleLatency) <= 0.45 else { return nil }
        let target = trackedBox.flatMap { LeadVehicle.validBox($0) ? $0 : nil }
            ?? preferredBox.flatMap { box -> CGRect? in
                guard LeadVehicle.validBox(box), let seen = lastDetectedAt,
                      seen.isFinite, time >= seen, time - seen <= 0.90 else { return nil }
                return box
            }
        let region: CGRect
        if let target {
            guard !vehicles.contains(where: { $0.confidence >= 0.65 && Self.overlap($0.box, target) >= 0.35
                && VehicleRangeHistory.distance(for: $0, horizontalFOV: 60) != nil }) else { return nil }
            let width = min(0.80, max(0.24, target.width * 2.6))
            let height = min(0.90, max(0.30, target.height * 2.6))
            region = CGRect(x: max(0, min(1 - width, target.midX - width / 2)),
                            y: max(0, min(1 - height, target.midY - height / 2)), width: width, height: height)
        } else {
            guard !vehicles.contains(where: { vehicle in
                guard ["car", "truck", "bus"].contains(vehicle.label), vehicle.confidence >= 0.65 else { return false }
                // A strong car in the adjacent lane must not suppress the
                // search for a missing lead in a freshly measured ego lane.
                guard let lane, lane.isUsable(at: time), lane.confidence >= 0.70,
                      let bounds = lane.bounds(at: Double(vehicle.box.minY)) else {
                    return LeadVehicleGeometry.supportsBlindLead(vehicle)
                }
                let overlap = max(0, min(Double(vehicle.box.maxX), bounds.right)
                    - max(Double(vehicle.box.minX), bounds.left)) / Double(vehicle.box.width)
                return overlap >= 0.20
            }) else { return nil }
            let bounds = lane.flatMap { $0.isUsable(at: time) ? $0.bounds(at: 0.42) : nil }
            let center = bounds.map { ($0.left + $0.right) / 2 } ?? 0.5
            region = CGRect(x: max(0, min(0.36, center - 0.32)), y: 0.08, width: 0.64, height: 0.72)
        }
        guard let crop = VehicleSearchCrop(region: region, imageAspect: imageAspect) else { return nil }
        lastAttempt = time
        return crop
    }

    public static func merge(fullFrame: [LeadVehicle], recovery: [LeadVehicle], imageAspect: Double) -> [LeadVehicle] {
        var result = fullFrame
        for found in recovery.sorted(by: { $0.confidence > $1.confidence }) {
            let wheel = ["motorcycle", "motorbike"].contains(found.label)
            let duplicates = result.indices.filter { i in
                let old = result[i]
                guard wheel == ["motorcycle", "motorbike"].contains(old.label) else { return false }
                return overlap(old.box, found.box) >= 0.55
                    && abs(old.box.midX - found.box.midX) <= min(old.box.width, found.box.width) * 0.25
                    && abs(old.box.midY - found.box.midY) <= min(old.box.height, found.box.height) * 0.25
            }
            // Multiple overlaps are ambiguous, not proof that they are one car.
            if duplicates.count > 1 { continue }
            if let i = duplicates.first {
                if result[i].confidence < 0.65 && found.confidence > result[i].confidence { result[i] = found }
            } else { result.append(found) }
        }
        return VehicleDetectionFilter.filter(result, imageAspect: imageAspect)
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
    }
}
