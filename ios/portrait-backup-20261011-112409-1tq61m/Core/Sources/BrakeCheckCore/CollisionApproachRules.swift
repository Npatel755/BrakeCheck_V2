import Foundation

/// Camera closing-time indication for an authorized collision hazard. TTC
/// uses range / positive range closure, so it does not require target braking
/// or absolute lead speed. Path, freshness and approach checks belong to the
/// caller. This remains a heuristic indication, never automatic braking.
public enum CollisionApproachRules {
    public static func combine(lead: RiskSnapshot, approach: RiskSnapshot) -> RiskSnapshot {
        guard approach.score != nil else { return lead }
        guard let leadScore = lead.score else { return approach }
        var result = lead
        if let approachScore = approach.score, approachScore > leadScore {
            result.level = approach.level; result.score = approachScore
            result.closingSpeed = approach.closingSpeed
            result.timeToCollision = approach.timeToCollision
            result.reason = approach.reason
        }
        return result
    }

    /// The same confirmed lead can supply camera TTC without waiting for a
    /// separate hazard selection or an absolute GPS speed estimate. The caller
    /// has already checked capture age; these gates prevent coasting, weak rear
    /// candidates and unsupported paths from acquiring camera risk.
    public static func forConfirmedLead(lead: RiskSnapshot, camera: CameraRangeSnapshot,
                                        confidence: Double, fresh: Bool, pathSupported: Bool,
                                        rearConfirmed: Bool) -> RiskSnapshot {
        guard fresh, pathSupported, rearConfirmed, let distance = camera.distance else { return lead }
        let approach = evaluate(distance: distance, closingSpeed: camera.closingSpeed,
            confidence: confidence, egoSpeed: nil, canEstimateLeadSpeed: false)
        return combine(lead: lead, approach: approach)
    }

    public static func evaluate(distance: Double, closingSpeed: Double?, confidence: Double,
                                egoSpeed: Double?, canEstimateLeadSpeed: Bool,
                                isCameraProjectedPath: Bool = false) -> RiskSnapshot {
        let validDistance = distance.isFinite && (1...150).contains(distance)
        guard validDistance, confidence.isFinite, (0.65...1).contains(confidence),
              let closingSpeed, closingSpeed.isFinite, (0.5...60).contains(closingSpeed) else {
            return RiskSnapshot(distance: validDistance ? distance : nil,
                                reason: "Waiting for a reliable closing-gap measurement")
        }
        let ttc = distance / closingSpeed
        let score: Double = ttc < 1.5 ? 95 : ttc < 3 ? 78 : ttc < 5 ? 52 : 0
        let level: RiskLevel = score >= 85 ? .critical : score >= 60 ? .high : score >= 30 ? .elevated : .low
        var leadSpeed: Double?
        if canEstimateLeadSpeed, let egoSpeed, egoSpeed.isFinite, (0...90).contains(egoSpeed) {
            let estimate = egoSpeed - closingSpeed
            if (-2...90).contains(estimate) { leadSpeed = max(0, estimate) }
        }
        return RiskSnapshot(level: level, score: score, distance: distance, closingSpeed: closingSpeed,
            leadSpeed: leadSpeed, timeToCollision: ttc,
            reason: isCameraProjectedPath
                ? "Closing on a possible obstacle in the camera-projected path. Lane position is uncertain."
                : "Closing on a vehicle in the driving path. Target braking is not required.")
    }
}
