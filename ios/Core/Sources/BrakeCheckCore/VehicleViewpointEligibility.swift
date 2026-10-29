import Foundation

/// Source pixels, before the 224-pixel classifier resize. Small but visible
/// rear views can be checked; upscaling a few pixels cannot qualify a crop.
public enum VehicleViewpointEligibility {
    public static func canClassify(_ vehicle: LeadVehicle, sourceWidth: Double, sourceHeight: Double) -> Bool {
        sourceWidth.isFinite && sourceHeight.isFinite && sourceWidth > 0 && sourceHeight > 0
            && ["car", "truck"].contains(vehicle.label)
            && vehicle.confidence.isFinite && (0.40...1).contains(vehicle.confidence)
            && LeadVehicle.validBox(vehicle.box) && vehicle.box.minY < 0.75
            && vehicle.box.minX > 0.005 && vehicle.box.maxX < 0.995
            && vehicle.box.minY > 0.005 && vehicle.box.maxY < 0.995
            && Double(vehicle.box.width) * sourceWidth >= 24
            && Double(vehicle.box.height) * sourceHeight >= 16
    }
}
