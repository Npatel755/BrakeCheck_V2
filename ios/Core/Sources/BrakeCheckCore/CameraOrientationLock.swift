import Foundation

public struct CameraOrientationRecord: Codable, Equatable {
    public let signature: String
    public let estimate: CameraCalibrationEstimate
}

/// A mount pose is configuration, not a live detection. Missing lanes do not
/// erase it. Rendering still checks fresh road/lane support independently.
public struct CameraOrientationLock {
    public private(set) var record: CameraOrientationRecord?
    public init(restored: CameraOrientationRecord? = nil) {
        if let restored, !restored.signature.isEmpty, restored.signature != "unavailable", Self.valid(restored.estimate) { record = restored }
    }
    public mutating func reset() { record = nil }
    public mutating func update(_ estimate: CameraCalibrationEstimate?, signature: String,
                                observedAt time: TimeInterval) -> CameraCalibrationEstimate? {
        guard signature != "unavailable", !signature.isEmpty else { return nil }
        if let record, record.signature != signature { reset() }
        // Saved orientation stays fixed even on bends, crests and missed lanes.
        // Confidence can suppress guidance without discarding configuration.
        if let record { return record.estimate }
        guard let estimate, Self.valid(estimate), time.isFinite else { return nil }
        record = CameraOrientationRecord(signature: signature, estimate: estimate)
        return estimate
    }
    public static func agrees(_ a: CameraCalibrationEstimate, _ b: CameraCalibrationEstimate) -> Bool {
        valid(a) && valid(b) && abs(a.horizon - b.horizon) <= 0.05 && abs(a.yawDegrees - b.yawDegrees) <= 3
    }
    private static func valid(_ pose: CameraCalibrationEstimate) -> Bool {
        [pose.horizon, pose.yawDegrees, pose.pitchDegrees, pose.confidence].allSatisfy(\.isFinite)
            && (0.35...0.9).contains(pose.horizon) && abs(pose.yawDegrees) <= 30
            && abs(pose.pitchDegrees) <= 60 && (0.7...1).contains(pose.confidence)
    }
}
