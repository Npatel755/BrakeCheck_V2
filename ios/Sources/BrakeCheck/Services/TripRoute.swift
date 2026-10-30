import BrakeCheckCore
import Foundation

/// Collection stays on the UI actor; encoding and persistence belong to TripArchiveStore.
@MainActor final class TripRouteRecorder {
    private var accumulator = TripRouteAccumulator()
    private var recording = false
    func start(destination: String?) {
        accumulator = TripRouteAccumulator(destination: destination)
        recording = true
    }
    func updateDestination(_ destination: String?) {
        guard recording else { return }
        accumulator.updateDestination(destination)
    }
    func cancel() { recording = false; accumulator = TripRouteAccumulator() }
    func add(_ location: CrashLocation) {
        guard recording else { return }
        accumulator.add(.init(latitude: location.latitude, longitude: location.longitude, time: location.measuredAt))
    }
    func add(_ kind: TripRoute.Kind, at location: CrashLocation?, speedMPH: Double?, closingSeconds: Double?) {
        guard recording else { return }
        accumulator.add(kind, at: Date(), location: location.map {
            .init(latitude: $0.latitude, longitude: $0.longitude, time: $0.measuredAt)
        }, speedMPH: speedMPH, closingSeconds: closingSeconds)
    }
    func finish() -> TripRoute {
        let route = accumulator.route
        cancel()
        return route
    }
}
