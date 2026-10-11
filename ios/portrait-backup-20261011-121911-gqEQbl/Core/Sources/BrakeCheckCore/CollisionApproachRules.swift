import Foundation

/// Camera closing-time indication for an authorized collision hazard. TTC
/// uses range / positive range closure, so it does not require target braking
/// or absolute lead speed. Path, freshness and approach checks belong to the
/// caller. A confirmed lead does not need lane support for relative motion.
/// This remains a heuristic indication, never automatic braking.
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
        if let ttc = approach.timeToCollision, ttc < (result.timeToCollision ?? .infinity) {
            result.timeToCollision = ttc
            result.closingSpeed = approach.closingSpeed
        }
        return result
    }

    /// The same confirmed lead can supply camera TTC without waiting for a
    /// separate hazard selection or an absolute GPS speed estimate. The caller
    /// has already checked capture age; these gates prevent coasting and weak
    /// rear candidates from acquiring camera risk. Path is optional evidence.
    public static func forConfirmedLead(lead: RiskSnapshot, camera: CameraRangeSnapshot,
                                        confidence: Double, fresh: Bool, pathSupported: Bool,
                                        rearConfirmed: Bool) -> RiskSnapshot {
        guard fresh, rearConfirmed else { return lead }
        let approach = evaluateCamera(camera, confidence: confidence, isCameraProjectedPath: !pathSupported)
        return combine(lead: lead, approach: approach)
    }

    /// A measured steady/opening gap gives a current lower indication, while
    /// missing motion remains unavailable. Reliable increasing closure can
    /// shorten TTC using a constant-acceleration projection of at most 3 s.
    public static func evaluateCamera(_ camera: CameraRangeSnapshot, confidence: Double,
                                      fallbackClosingSpeed: Double? = nil, egoSpeed: Double? = nil,
                                      canEstimateLeadSpeed: Bool = false,
                                      isCameraProjectedPath: Bool = false) -> RiskSnapshot {
        guard let distance = camera.distance, distance.isFinite, (1...150).contains(distance),
              confidence.isFinite, (0.65...1).contains(confidence),
              let closing = camera.closingSpeed ?? fallbackClosingSpeed,
              closing.isFinite, (0...60).contains(closing) else {
            let validDistance = camera.distance.flatMap { $0.isFinite && (1...150).contains($0) ? $0 : nil }
            return RiskSnapshot(distance: validDistance,
                reason: "Waiting for a reliable gap-motion measurement")
        }
        var result: RiskSnapshot
        if closing > 0.5 {
            result = evaluate(distance: distance, closingSpeed: closing, confidence: confidence,
                egoSpeed: egoSpeed, canEstimateLeadSpeed: canEstimateLeadSpeed,
                isCameraProjectedPath: isCameraProjectedPath)
        } else {
            result = RiskSnapshot(level: .low, score: 0, distance: distance, closingSpeed: closing,
                reason: "No elevated indication in the current camera gap motion.")
        }
        guard let acceleration = camera.closingAcceleration, acceleration.isFinite,
              (0.8...12).contains(acceleration), let rate = camera.rangeRate,
              rate.isFinite, abs(rate) <= 60, abs(closing - max(0, -rate)) <= 0.001 else { return result }
        let velocity = -rate
        let discriminant = velocity * velocity + 2 * acceleration * distance
        let projectedTTC = 2 * distance / (velocity + sqrt(discriminant))
        guard projectedTTC.isFinite, projectedTTC > 0, projectedTTC <= 3,
              projectedTTC < (result.timeToCollision ?? .infinity) else { return result }
        result.timeToCollision = projectedTTC
        result.score = projectedTTC < 1.5 ? 95 : projectedTTC < 3 ? 78 : 52
        result.level = projectedTTC < 1.5 ? .critical : projectedTTC < 3 ? .high : .elevated
        result.reason = isCameraProjectedPath
            ? "Relative gap acceleration indicates a possible collision ahead. Lane position is uncertain."
            : "Increasing gap closure shortens the estimated collision time. Relative acceleration is camera measured."
        return result
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
