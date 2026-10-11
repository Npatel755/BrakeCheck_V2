import Foundation

/// Save the accurate visual tracker for weak/missing or ambiguous detections.
/// This gate never creates a detection, ID, orientation or numeric sample.
public enum VehicleTrackSupport {
    public static func needsVisualTracking(_ vehicles: [LeadVehicle], previousBox: CGRect?) -> Bool {
        guard let previous = previousBox, LeadVehicle.validBox(previous) else { return true }
        let overlaps = vehicles.filter { overlap($0.box, previous) >= 0.20 }
        guard overlaps.count == 1, let found = overlaps.first,
              found.confidence.isFinite, (0.65...1).contains(found.confidence),
              VehicleRangeHistory.distance(for: found, horizontalFOV: 60) != nil,
              (0.75...1.33).contains(Double(found.box.width / previous.width)),
              (0.75...1.33).contains(Double(found.box.height / previous.height)),
              overlap(found.box, previous) >= 0.50 else { return true }
        return false
    }
    public static func reliableScaleGrowth(from previous: LeadVehicle, to current: LeadVehicle,
                                          at time: TimeInterval) -> Bool {
        guard let id = previous.trackID, current.trackID == id,
              LeadVehicle.validBox(previous.box), current.confidence.isFinite, (0.65...1).contains(current.confidence),
              VehicleRangeHistory.distance(for: current, horizontalFOV: 60) != nil,
              current.travel?.capturedAt == time,
              current.travel?.supportsCollisionApproach(at: time) == true,
              current.travel?.hasStableBearing == true else { return false }
        let width = Double(current.box.width / previous.box.width)
        let height = Double(current.box.height / previous.box.height)
        return (1...1.65).contains(width) && (1...1.65).contains(height)
            && (0.90...1.10).contains(width / height)
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        guard LeadVehicle.validBox(a), LeadVehicle.validBox(b) else { return 0 }
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
    }
}
