import Foundation

/// Camera-only measurements of one selected vehicle. These are separate from
/// driving-path authorization, GPS, collision risk, and warning delivery.
public struct CameraRangeSnapshot {
    public var distance: Double?
    public var closingSpeed: Double?
    public var rangeRate: Double?
    public var closingAcceleration: Double?
    public var reason: String

    public init(distance: Double? = nil, closingSpeed: Double? = nil,
                rangeRate: Double? = nil, closingAcceleration: Double? = nil,
                reason: String = "Waiting for a fresh vehicle measurement") {
        self.distance = distance
        self.closingSpeed = closingSpeed
        self.rangeRate = rangeRate
        self.closingAcceleration = closingAcceleration
        self.reason = reason
    }
}

public struct CameraRangeEstimator {
    private var targetID: UUID?
    private var distances: [TimedValue] = []
    private var lastCapture: TimeInterval?
    private var pendingRange: TimedValue?

    public init() {}

    public mutating func reset() {
        targetID = nil
        distances.removeAll()
        lastCapture = nil
        pendingRange = nil
    }

    /// A missing/low-confidence observation never becomes a zero speed. A
    /// brief gap can keep the sample history, but its output is unavailable.
    public mutating func update(targetID id: UUID, time: TimeInterval,
                                distance: Double, confidence: Double,
                                fresh: Bool) -> CameraRangeSnapshot {
        if targetID != id { reset(); targetID = id }
        guard time.isFinite else {
            reset()
            return CameraRangeSnapshot(reason: "Camera timestamp invalid")
        }
        if let previous = lastCapture, time <= previous {
            reset()
            return CameraRangeSnapshot(reason: "Waiting for ordered camera measurements")
        }
        if let previous = lastCapture, time - previous > 0.6 + 1e-6 {
            distances.removeAll(); pendingRange = nil
        }
        lastCapture = time
        // Predict from the preceding complete window before the current
        // timestamp evicts its oldest sample (important at 2 Hz).
        let previousMotion = distances.last.flatMap { CameraMotionFit.estimate(distances, distance: $0.value) }
        let window = TemporalSampling.window(times: distances.map(\.time) + [time],
            minimumSamples: 5, minimumWindow: 1.5, maximumWindow: 2.5)
        distances.removeAll { time - $0.time > window + 1e-6 }
        guard fresh else {
            return CameraRangeSnapshot(reason: "Detection gap · camera numbers paused")
        }
        guard distance.isFinite, (1...150).contains(distance), confidence.isFinite,
              (0...1).contains(confidence) else {
            reset()
            return CameraRangeSnapshot(reason: "Vehicle range unavailable")
        }
        guard confidence >= 0.65 else {
            return CameraRangeSnapshot(distance: distance, reason: "Gap available · motion confidence low")
        }
        if let previous = distances.last {
            if time - previous.time > 0.6 + 1e-6 { distances.removeAll() }
            else {
                let dt = time - previous.time
                let expected = previous.value + (previousMotion?.rangeRate ?? 0) * dt
                    - 0.5 * (previousMotion?.closingAcceleration ?? 0) * dt * dt
                if abs(distance - expected) > max(2, previous.value * 0.15) {
                    // One bad crop must not poison the range window. Two new,
                    // consistent captures establish a changed scale and restart it.
                    if let pending = pendingRange, (0.08...0.60).contains(time - pending.time),
                       abs(distance - pending.value) <= max(2, pending.value * 0.10) {
                        distances.removeAll()
                    } else {
                        pendingRange = TimedValue(time: time, value: distance)
                        return CameraRangeSnapshot(reason: "Range changed abruptly · checking next capture")
                    }
                }
            }
        }
        pendingRange = nil
        distances.append(TimedValue(time: time, value: distance))
        guard distances.count >= 5, let first = distances.first,
              time - first.time >= 0.9 - 1e-6 else {
            return CameraRangeSnapshot(distance: distance, reason: "Gap available · sampling gap change")
        }
        guard let motion = CameraMotionFit.estimate(distances, distance: distance) else {
            return CameraRangeSnapshot(distance: distance, reason: "Gap available · range change unsettled")
        }
        let rate = motion.rangeRate
        return CameraRangeSnapshot(distance: distance, closingSpeed: max(0, -rate),
            rangeRate: rate, closingAcceleration: motion.closingAcceleration,
            reason: (motion.closingAcceleration ?? 0) > 0.8 ? "Gap closure accelerating"
                : rate > 0.5 ? "Gap opening" : rate < -0.5 ? "Gap closing" : "Gap steady")
    }
}
