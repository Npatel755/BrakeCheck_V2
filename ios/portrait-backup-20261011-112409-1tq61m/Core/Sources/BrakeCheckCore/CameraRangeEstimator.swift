import Foundation

/// Camera-only measurements of one selected vehicle. These are separate from
/// driving-path authorization, GPS, collision risk, and warning delivery.
public struct CameraRangeSnapshot {
    public var distance: Double?
    public var closingSpeed: Double?
    public var reason: String

    public init(distance: Double? = nil, closingSpeed: Double? = nil,
                reason: String = "Waiting for a fresh vehicle measurement") {
        self.distance = distance
        self.closingSpeed = closingSpeed
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
            else if abs(distance - previous.value) > max(2, previous.value * 0.15) {
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
        pendingRange = nil
        distances.append(TimedValue(time: time, value: distance))
        guard let rate = DriveMath.slope(distances, minimumSpan: 0.9),
              distances.count >= 5 else {
            return CameraRangeSnapshot(distance: distance, reason: "Gap available · sampling gap change")
        }
        // Reject a wobbling/size-changing box even if its average slope is small.
        let first = distances[0]
        let intercept = distances.reduce(0) { $0 + $1.value - rate * ($1.time - first.time) }
            / Double(distances.count)
        let variance = distances.reduce(0) {
            $0 + pow($1.value - (intercept + rate * ($1.time - first.time)), 2)
        } / Double(distances.count)
        guard rate.isFinite, abs(rate) <= 60,
              sqrt(variance) <= max(0.25, distance * 0.025) else {
            return CameraRangeSnapshot(distance: distance, reason: "Gap available · range change unsettled")
        }
        return CameraRangeSnapshot(distance: distance, closingSpeed: max(0, -rate),
            reason: rate > 0.5 ? "Gap opening" : rate < -0.5 ? "Gap closing" : "Gap steady")
    }
}
