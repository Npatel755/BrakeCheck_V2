import Foundation

/// Missing/weak captures hide estimates but need not erase reliable samples.
/// History crosses at most 600 ms without a reliable sample, never a new ID
/// (the caller resets on target, camera geometry, or detector changes).
public struct LeadRiskContinuity {
    private var estimator = RiskEstimator()
    private var lastReliable: TimeInterval?
    private var lastInput: TimeInterval?
    public init() {}
    public mutating func reset() {
        estimator.reset(); lastReliable = nil; lastInput = nil
    }
    public mutating func pause(at time: TimeInterval, reason: String) -> RiskSnapshot {
        guard time.isFinite, lastInput.map({ time > $0 }) ?? true else {
            reset(); return RiskSnapshot(reason: reason)
        }
        if let reliable = lastReliable, time - reliable > 0.60 { reset() }
        lastInput = time
        return RiskSnapshot(reason: reason)
    }
    public mutating func update(time: TimeInterval, distance: Double, egoSpeed: Double?,
                                gpsAge: TimeInterval, confidence: Double,
                                camera: CameraRangeSnapshot? = nil) -> RiskSnapshot {
        guard time.isFinite, lastInput.map({ time > $0 }) ?? true,
              distance.isFinite, (1...150).contains(distance),
              confidence.isFinite, (0...1).contains(confidence) else {
            reset(); return RiskSnapshot(reason: "Waiting for ordered, reliable camera measurements.")
        }
        if confidence < 0.65 {
            return pause(at: time, reason: "Tracking confidence low. Estimates paused.")
        }
        lastInput = time
        let result = estimator.update(time: time, distance: distance, egoSpeed: egoSpeed,
                                      gpsAge: gpsAge, confidence: confidence, camera: camera)
        // No GPS or rejected geometry must not preserve absolute-speed history.
        if result.distance == nil { lastReliable = nil }
        else { lastReliable = time }
        return result
    }
}
