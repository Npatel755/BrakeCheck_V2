import Foundation

/// Display state only. It carries no distance, speed, TTC or warning authority.
public struct DangerMeterSnapshot {
    public var level: RiskLevel
    public var score: Double?
    public var isHeld: Bool

    public init(level: RiskLevel = .unknown, score: Double? = nil, isHeld: Bool = false) {
        self.level = level; self.score = score; self.isHeld = isHeld
    }
}

/// Keep the danger steps legible across brief misses of the same lead. Rising
/// risk is immediate; a lower band needs 350 ms of new, lower observations.
/// An absent estimate never renews the hold, which expires 600 ms after the
/// last measurement. Actual numeric readouts keep their own strict validity.
public struct LeadDangerContinuity {
    public static let holdSeconds: TimeInterval = 0.6
    public static let declineConfirmationSeconds: TimeInterval = 0.35
    private var targetID: UUID?
    private var displayed = DangerMeterSnapshot()
    private var lastMeasurement: TimeInterval?
    private var lowerSince: TimeInterval?

    public init() {}

    public mutating func reset() {
        targetID = nil; displayed = DangerMeterSnapshot()
        lastMeasurement = nil; lowerSince = nil
    }

    public mutating func update(targetID id: UUID?, capturedAt: TimeInterval,
                                now: TimeInterval, risk: RiskSnapshot) -> DangerMeterSnapshot {
        guard let id, capturedAt.isFinite, now.isFinite,
              capturedAt <= now, now - capturedAt <= Self.holdSeconds + 1e-6 else {
            reset(); return displayed
        }
        if id != targetID { reset(); targetID = id }
        if let lastMeasurement, capturedAt < lastMeasurement {
            reset(); targetID = id
            return displayed
        }
        if let score = risk.score, score.isFinite, (0...100).contains(score), risk.level != .unknown {
            let newCapture = lastMeasurement.map { capturedAt > $0 } ?? true
            if let lastMeasurement, capturedAt - lastMeasurement > Self.holdSeconds + 1e-6 {
                displayed = DangerMeterSnapshot(); lowerSince = nil
            }
            if let previousScore = displayed.score, band(score) < band(previousScore) {
                if lowerSince == nil { lowerSince = capturedAt }
                if newCapture, capturedAt - (lowerSince ?? capturedAt) >= Self.declineConfirmationSeconds - 1e-6 {
                    displayed = DangerMeterSnapshot(level: risk.level, score: score)
                    lowerSince = nil
                } else { displayed.isHeld = true }
            } else {
                displayed = DangerMeterSnapshot(level: risk.level, score: score)
                lowerSince = nil
            }
            lastMeasurement = capturedAt
            return displayed
        }
        // A miss interrupts evidence for lowering, but never updates the age
        // of the last accepted score or turns an unknown estimate into green.
        lowerSince = nil
        guard let lastMeasurement, now - lastMeasurement <= Self.holdSeconds + 1e-6 else {
            displayed = DangerMeterSnapshot(); return displayed
        }
        displayed.isHeld = displayed.score != nil
        return displayed
    }

    private func band(_ score: Double) -> Int {
        score >= 85 ? 3 : score >= 60 ? 2 : score >= 30 ? 1 : 0
    }
}
