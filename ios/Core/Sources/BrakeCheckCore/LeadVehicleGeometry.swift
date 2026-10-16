import Foundation

/// Forward footprint for a fixed, centered camera. This is mounting geometry,
/// not a detected lane; a fresh measured lane can support curved-road leads.
public enum LeadVehicleGeometry {
    public static func forwardOverlap(_ vehicle: LeadVehicle) -> Double {
        guard LeadVehicle.validBox(vehicle.box) else { return 0 }
        let half = Double(vehicle.box.width) * 1.9 / vehicle.assumedWidthMeters / 2
        let intersection = max(0, min(Double(vehicle.box.maxX), 0.5 + half)
            - max(Double(vehicle.box.minX), 0.5 - half))
        return intersection / Double(vehicle.box.width)
    }
    public static func supportsBlindLead(_ vehicle: LeadVehicle) -> Bool {
        guard LeadVehicle.validBox(vehicle.box), vehicle.box.minY < 0.75 else { return false }
        let half = Double(vehicle.box.width) * 1.9 / vehicle.assumedWidthMeters / 2
        return forwardOverlap(vehicle) >= 0.35 && abs(Double(vehicle.box.midX) - 0.5) <= half + 0.015
    }
}
