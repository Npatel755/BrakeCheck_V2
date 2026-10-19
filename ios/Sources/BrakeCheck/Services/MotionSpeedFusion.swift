import Foundation
import CoreMotion

/// Experimental speed estimate for display and testing only.
/// Do not use this output as a safety-critical collision-warning input.
struct MotionSpeedEstimate {
    let metersPerSecond: Double
    let uncertaintyMetersPerSecond: Double
    let gpsAgeSeconds: TimeInterval
    let source: String
    var milesPerHour: Double { metersPerSecond * 2.2369362921 }
}

/// Scalar Kalman filter for signed forward speed.
/// Acceleration fusion is opt-in because an arbitrary phone orientation
/// does NOT tell us which axis points forward in the vehicle.
/// Create/use on main thread, including calls to acceptGPS.
final class MotionSpeedFusion {
    enum ForwardAxis { case xPositive, xNegative, yPositive, yNegative, zPositive, zNegative }

    private let motion = CMMotionManager()
    private let motionQueue: OperationQueue = {
        let q = OperationQueue()
        q.name = "BrakeCheck.motion"
        q.maxConcurrentOperationCount = 1
        q.qualityOfService = .userInitiated
        return q
    }()

    // Keep all estimate state on main queue.
    private var velocity: Double = 0
    private var variance: Double = 100
    private var lastPredictionTime: TimeInterval?
    private var lastGPSArrival: TimeInterval?
    private var lastGPSMeasurementTime: TimeInterval?
    private var axis: ForwardAxis? // nil = GPS-only
    private var running = false

    /// Status and estimate callbacks run on main thread.
    var onEstimate: ((MotionSpeedEstimate?) -> Void)?
    var onStatus: ((String) -> Void)?

    /// Maximum age of the *last GPS measurement*, even while IMU predicts.
    private let maximumGPSAge: TimeInterval = 1.5
    private let maxPredictionStep: TimeInterval = 0.1
    private let maxAcceleration: Double = 5.0 // m/s²; cap motion artifacts
    private let processNoise: Double = 2.0 // variance growth (m/s)^2 / sec

    /// Pass nil for GPS-only filtering. With a mounted phone, select an axis
    /// only AFTER verifying which direction is car-forward. Incorrect axis
    /// calibration will make the fused estimate worse.
    func configureForwardAxis(_ newAxis: ForwardAxis?) {
        dispatchPrecondition(condition: .onQueue(.main))
        axis = newAxis
        if running { start() } else { reset() }
        onStatus?(newAxis == nil ? "GPS-only Kalman mode" : "Experimental motion fusion enabled; verify mount orientation")
    }

    func start() {
        dispatchPrecondition(condition: .onQueue(.main))
        stop()
        reset()
        running = true
        guard axis != nil else {
            onStatus?("GPS-only Kalman mode · motion sensor off")
            return
        }
        guard motion.isDeviceMotionAvailable else {
            onStatus?("Device motion unavailable; GPS-only mode")
            return
        }
        motion.deviceMotionUpdateInterval = 1.0 / 20.0
        motion.startDeviceMotionUpdates(to: motionQueue) { [weak self] sample, error in
            guard let self, let sample, error == nil else { return }
            let raw = sample.userAcceleration
            let stamp = ProcessInfo.processInfo.systemUptime
            OperationQueue.main.addOperation { [weak self] in
                guard let self, self.running else { return }
                self.predict(acceleration: self.acceleration(from: raw), at: stamp)
            }
        }
    }

    func stop() {
        dispatchPrecondition(condition: .onQueue(.main))
        running = false
        motion.stopDeviceMotionUpdates()
        reset()
        onEstimate?(nil)
    }

    private func reset() {
        velocity = 0
        variance = 100
        lastPredictionTime = nil
        lastGPSArrival = nil
        lastGPSMeasurementTime = nil
    }

    /// Call with Core Location's raw speed + speedAccuracy, not with an
    /// already-smoothed or held GPS value. All speeds are meters/second.
    func acceptGPS(speed: Double, accuracy: Double, age: TimeInterval) {
        dispatchPrecondition(condition: .onQueue(.main))
        guard running,
              speed.isFinite, speed >= 0,
              accuracy.isFinite, accuracy >= 0, accuracy <= 5,
              age.isFinite, age >= -0.5, age <= 1.5 else { return }

        let now = ProcessInfo.processInfo.systemUptime
        // Prevent treating repeated identical GPS fixes as independent updates.
        let measurementTime = now - max(0, age)
        if let previous = lastGPSMeasurementTime,
           measurementTime <= previous + 0.02 { return }
        lastGPSMeasurementTime = measurementTime
        lastGPSArrival = now

        let measurementVariance = max(0.25, accuracy * accuracy)
        if variance >= 90 { // first trustworthy GPS observation
            velocity = speed
            variance = measurementVariance
        } else {
            // Reject impossible jumps relative to stated uncertainty.
            let innovation = speed - velocity
            let innovationSD = sqrt(variance + measurementVariance)
            guard abs(innovation) <= max(7.0, 4.0 * innovationSD) else {
                onStatus?("GPS spike rejected")
                return
            }
            let gain = variance / (variance + measurementVariance)
            velocity = max(0, velocity + gain * innovation)
            variance = max(0.01, (1 - gain) * variance)
        }
        lastPredictionTime = now
        publish(at: now)
    }

    private func acceleration(from a: CMAcceleration) -> Double? {
        guard let axis else { return nil }
        switch axis {
        case .xPositive: return a.x
        case .xNegative: return -a.x
        case .yPositive: return a.y
        case .yNegative: return -a.y
        case .zPositive: return a.z
        case .zNegative: return -a.z
        }
    }

    private func predict(acceleration: Double?, at time: TimeInterval) {
        guard let lastGPSArrival else { return }
        guard time - lastGPSArrival <= maximumGPSAge else {
            onEstimate?(nil)
            onStatus?("Speed estimate stale; waiting for GPS")
            return
        }
        guard let last = lastPredictionTime else {
            lastPredictionTime = time
            return
        }
        let dt = time - last
        guard dt > 0, dt <= maxPredictionStep else {
            lastPredictionTime = time
            publish(at: time)
            return
        }
        lastPredictionTime = time
        if let acceleration, acceleration.isFinite {
            let a = min(max(acceleration, -maxAcceleration), maxAcceleration)
            velocity = max(0, velocity + a * dt)
            // Motion measurements drift: increase model uncertainty rapidly.
            variance += processNoise * dt + 0.2 * a * a * dt
        } else {
            variance += processNoise * dt
        }
        publish(at: time)
    }

    private func publish(at now: TimeInterval) {
        guard let received = lastGPSArrival,
              let measured = lastGPSMeasurementTime else { return }
        let gpsAge = max(0, now - measured)
        guard now - received <= maximumGPSAge, gpsAge <= 2.5 else {
            onEstimate?(nil)
            return
        }
        onEstimate?(MotionSpeedEstimate(
            metersPerSecond: velocity,
            uncertaintyMetersPerSecond: sqrt(max(variance, 0)),
            gpsAgeSeconds: gpsAge,
            source: axis == nil ? "GPS Kalman" : "GPS + IMU (experimental)"
        ))
    }
}
