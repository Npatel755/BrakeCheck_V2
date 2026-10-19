import Foundation

/// Core ML queues share the phone's accelerator. Vehicles wait only for the
/// prediction already running; auxiliary predictions never queue behind them.
final class VisionInferenceCoordinator {
    private let predictionLock = NSLock()
    private let stateLock = NSLock()
    private var waitingVehicles = 0

    func vehicle<T>(_ operation: () throws -> T) rethrows -> T {
        stateLock.lock(); waitingVehicles += 1; stateLock.unlock()
        predictionLock.lock()
        stateLock.lock(); waitingVehicles -= 1; stateLock.unlock()
        defer { predictionLock.unlock() }
        return try operation()
    }

    func auxiliary<T>(_ operation: () throws -> T) rethrows -> T? {
        stateLock.lock()
        guard waitingVehicles == 0, predictionLock.try() else {
            stateLock.unlock(); return nil
        }
        stateLock.unlock()
        defer { predictionLock.unlock() }
        return try operation()
    }
}
