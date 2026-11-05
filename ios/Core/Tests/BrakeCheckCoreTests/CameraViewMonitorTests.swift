import XCTest
@testable import BrakeCheckCore

final class CameraViewMonitorTests: XCTestCase {
    private func sample(_ spread: Double?) -> DetectorLightingSample {
        DetectorLightingSample(medianLuma: 0.3, darkFraction: 0.2, spread: spread)
    }
    private func moving(at t: TimeInterval, mps: Double = 12, simulated: Bool = false, accuracy: Double = 5) -> GPSSpeedSample {
        GPSSpeedSample(metersPerSecond: mps, accuracy: accuracy, acceleration: 0, measurementUptime: t, simulated: simulated)
    }
    private func feed(_ monitor: inout CameraViewMonitor, spread: Double?, from start: Double, through end: Double,
                      speed: (TimeInterval) -> GPSSpeedSample?) {
        for t in stride(from: start, through: end, by: 0.5) { monitor.update(sample(spread), speed: speed(t), at: t) }
    }

    func testUniformFramesWhileMovingBlockAfterFifteenSeconds() {
        var monitor = CameraViewMonitor()
        feed(&monitor, spread: 0.02, from: 0, through: 14.5) { self.moving(at: $0) }
        XCTAssertFalse(monitor.blocked)
        XCTAssertTrue(monitor.update(sample(0.02), speed: moving(at: 15), at: 15))
        XCTAssertTrue(monitor.update(sample(0.02), speed: nil, at: 15.5), "holds while GPS is missing")
        XCTAssertFalse(monitor.update(sample(0.3), speed: moving(at: 16), at: 16), "a varied frame clears it")
    }

    func testStoppedSimulatedOrInaccurateGPSNeverBlocks() {
        var slow = CameraViewMonitor()
        feed(&slow, spread: 0.0, from: 0, through: 60) { self.moving(at: $0, mps: 1) }
        XCTAssertFalse(slow.blocked)
        var simulated = CameraViewMonitor()
        feed(&simulated, spread: 0.0, from: 0, through: 60) { self.moving(at: $0, simulated: true) }
        XCTAssertFalse(simulated.blocked)
        var inaccurate = CameraViewMonitor()
        feed(&inaccurate, spread: 0.0, from: 0, through: 60) { self.moving(at: $0, accuracy: 40) }
        XCTAssertFalse(inaccurate.blocked)
        var stale = CameraViewMonitor()
        feed(&stale, spread: 0.0, from: 0, through: 60) { self.moving(at: $0 - 10) }
        XCTAssertFalse(stale.blocked)
    }

    func testNeutralSpreadHoldsAndMissingSpreadResets() {
        var monitor = CameraViewMonitor()
        feed(&monitor, spread: 0.02, from: 0, through: 10) { self.moving(at: $0) }
        feed(&monitor, spread: 0.09, from: 10.5, through: 30) { self.moving(at: $0) }
        XCTAssertFalse(monitor.blocked, "the neutral band never confirms on its own")
        XCTAssertTrue(monitor.update(sample(0.02), speed: moving(at: 30.5), at: 30.5), "the original run still counts")
        XCTAssertFalse(monitor.update(sample(nil), speed: moving(at: 31), at: 31))
        XCTAssertFalse(monitor.blocked)
    }

    func testGapInSamplesRestartsTheCount() {
        var monitor = CameraViewMonitor()
        feed(&monitor, spread: 0.02, from: 0, through: 10) { self.moving(at: $0) }
        XCTAssertFalse(monitor.update(sample(0.02), speed: moving(at: 20), at: 20))
        feed(&monitor, spread: 0.02, from: 20.5, through: 34.5) { self.moving(at: $0) }
        XCTAssertFalse(monitor.blocked)
        XCTAssertTrue(monitor.update(sample(0.02), speed: moving(at: 35), at: 35))
    }
}
