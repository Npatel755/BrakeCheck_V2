import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Native NMS is class-specific. One car can otherwise arrive twice (car/bus)
/// and be mistaken for two competing identities by the association stage.
public enum VehicleDetectionFilter {
    public static func normalizedBox(_ box: CGRect) -> CGRect? {
        guard [box.minX, box.minY, box.width, box.height].allSatisfy({ $0.isFinite }),
              box.width > 0, box.height > 0,
              box.minX >= -0.02, box.minY >= -0.02,
              box.maxX <= 1.02, box.maxY <= 1.02 else { return nil }
        let clipped = box.intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
        return LeadVehicle.validBox(clipped) ? clipped : nil
    }

    public static func filter(_ detections: [LeadVehicle], imageAspect: Double) -> [LeadVehicle] {
        guard imageAspect.isFinite, (0.1...10).contains(imageAspect) else { return [] }
        let candidates = detections.compactMap { vehicle -> LeadVehicle? in
            guard vehicle.confidence.isFinite, (0.28...1).contains(vehicle.confidence),
                  ["car", "truck", "bus", "motorcycle", "motorbike"].contains(vehicle.label),
                  let box = normalizedBox(vehicle.box),
                  (0.20...4.5).contains(Double(box.width / box.height) * imageAspect) else { return nil }
            return LeadVehicle(box: box, label: vehicle.label, confidence: vehicle.confidence,
                viewpoint: vehicle.viewpoint, trackID: vehicle.trackID, travel: vehicle.travel)
        }.sorted { $0.confidence > $1.confidence }
        var accepted: [LeadVehicle] = []
        for vehicle in candidates {
            let twoWheels = ["motorcycle", "motorbike"].contains(vehicle.label)
            guard !accepted.contains(where: { existing in
                twoWheels == ["motorcycle", "motorbike"].contains(existing.label)
                    && overlap(vehicle.box, existing.box) >= 0.70
            }) else { continue }
            accepted.append(vehicle)
            if accepted.count == 64 { break }
        }
        return accepted
    }

    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
    }
}
