import Foundation

/// Vehicle boxes and their front/rear checks share the accelerator. A waiting
/// viewpoint check gets a turn after at most one vehicle prediction; lane
/// work uses idle capacity. No auxiliary camera-frame backlog is retained.
final class VisionInferenceCoordinator {
    private let condition = NSCondition()
    private var predicting = false
    private var waitingVehicles = 0
    private var waitingViewpoints = 0
    private var vehiclesSinceViewpoint = 0

    func vehicle<T>(_ operation: () throws -> T) rethrows -> T {
        condition.lock(); waitingVehicles += 1
        while predicting || (waitingViewpoints > 0 && vehiclesSinceViewpoint >= 1) {
            condition.wait()
        }
        waitingVehicles -= 1; predicting = true
        vehiclesSinceViewpoint = min(1, vehiclesSinceViewpoint + 1)
        condition.unlock()
        defer { finish() }
        return try operation()
    }

    /// Wait only for this crop's remaining freshness budget. Unlike an idle
    /// lane request, classification cannot repeatedly lose to new lane frames.
    func viewpoint<T>(until deadline: TimeInterval, _ operation: () throws -> T) rethrows -> T? {
        guard deadline.isFinite else { return nil }
        condition.lock(); waitingViewpoints += 1
        while true {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else {
                waitingViewpoints -= 1; condition.broadcast(); condition.unlock()
                return nil
            }
            if !predicting && (waitingVehicles == 0 || vehiclesSinceViewpoint >= 1) { break }
            _ = condition.wait(until: Date(timeIntervalSinceNow: remaining))
        }
        waitingViewpoints -= 1; predicting = true; vehiclesSinceViewpoint = 0
        condition.unlock()
        defer { finish() }
        return try operation()
    }

    func auxiliary<T>(_ operation: () throws -> T) rethrows -> T? {
        condition.lock()
        guard !predicting, waitingVehicles == 0, waitingViewpoints == 0 else {
            condition.unlock(); return nil
        }
        predicting = true; condition.unlock()
        defer { finish() }
        return try operation()
    }

    private func finish() {
        condition.lock(); predicting = false; condition.broadcast(); condition.unlock()
    }
}
