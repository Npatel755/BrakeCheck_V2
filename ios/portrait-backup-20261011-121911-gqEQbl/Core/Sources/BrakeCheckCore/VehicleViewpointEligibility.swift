import Foundation

/// Source pixels, before the 224-pixel classifier resize. Small but visible
/// rear views can be checked; upscaling a few pixels cannot qualify a crop.
public enum VehicleViewpointEligibility {
    public static func canClassify(_ vehicle: LeadVehicle, sourceWidth: Double, sourceHeight: Double) -> Bool {
        sourceWidth.isFinite && sourceHeight.isFinite && sourceWidth > 0 && sourceHeight > 0
            && ["car", "truck"].contains(vehicle.label)
            && vehicle.confidence.isFinite && (0.40...1).contains(vehicle.confidence)
            && LeadVehicle.validBox(vehicle.box) && vehicle.box.minY < 0.75
            && Double(vehicle.box.width) * sourceWidth >= 24
            && Double(vehicle.box.height) * sourceHeight >= 16
    }
}
