import XCTest
@testable import BrakeCheckCore

final class CameraSessionRecoveryTests: XCTestCase {
    func testTransientResetPausesMeasurementsUntilRestartAndReusesPendingRetry() throws {
        var camera = CameraSessionRecovery()
        camera.start(); camera.sessionStarted(at: 0)
        XCTAssertTrue(camera.acceptsFrames)
        let retry = try XCTUnwrap(camera.failed(at: 100))
        XCTAssertFalse(camera.acceptsFrames)
        XCTAssertEqual(camera.failed(at: 100.1), retry)
        XCTAssertFalse(camera.take(retry, at: 100.4))
        XCTAssertTrue(camera.take(retry, at: 100.5))
        XCTAssertFalse(camera.take(retry, at: 100.6))
        XCTAssertFalse(camera.acceptsFrames)
        camera.sessionStarted(at: 100.5)
        XCTAssertTrue(camera.acceptsFrames)
    }

    func testStopOrBackgroundCannotBeUndoneByOldRetry() throws {
        var camera = CameraSessionRecovery()
        camera.start()
        let old = try XCTUnwrap(camera.failed(at: 0))
        camera.stop()
        XCTAssertFalse(camera.take(old, at: 100))
        XCTAssertFalse(camera.endInterruption())
        camera.sessionStarted(at: 100)
        XCTAssertFalse(camera.acceptsFrames)
        camera.start()
        let current = try XCTUnwrap(camera.failed(at: 10))
        XCTAssertFalse(camera.take(old, at: 100))
        XCTAssertTrue(camera.take(current, at: 100))
    }

    func testInterruptionWaitsForSystemAndDoesNotRestartEndedTrip() throws {
        var camera = CameraSessionRecovery()
        camera.start(); camera.sessionStarted(at: 0)
        let pending = try XCTUnwrap(camera.failed(at: 0))
        camera.beginInterruption()
        XCTAssertFalse(camera.acceptsFrames)
        XCTAssertNil(camera.failed(at: 1))
        XCTAssertFalse(camera.take(pending, at: 10))
        XCTAssertTrue(camera.endInterruption())
        camera.sessionStarted(at: 10)
        XCTAssertTrue(camera.acceptsFrames)
        camera.beginInterruption(); camera.stop()
        XCTAssertFalse(camera.endInterruption())
    }

    func testRepeatedFailuresAreBoundedEvenIfOneFrameArrivesBetweenResets() throws {
        var camera = CameraSessionRecovery()
        camera.start()
        for (index, delay) in [0.5, 1.0, 2.0].enumerated() {
            let now = Double(index) * 2
            let retry = try XCTUnwrap(camera.failed(at: now))
            XCTAssertEqual(retry.at, now + delay)
            XCTAssertTrue(camera.take(retry, at: now + delay))
            camera.sessionStarted(at: now + delay); camera.frameReceived(at: now + delay)
        }
        XCTAssertNil(camera.failed(at: 20))
        XCTAssertFalse(camera.acceptsFrames)
        camera.start() // Explicit Retry camera starts a fresh recovery budget.
        XCTAssertNotNil(camera.failed(at: 30))
    }

    func testSustainedHealthyCaptureRestoresRetryBudget() throws {
        var camera = CameraSessionRecovery()
        camera.start()
        for time in [0.0, 2.0, 4.0] {
            let retry = try XCTUnwrap(camera.failed(at: time))
            XCTAssertTrue(camera.take(retry, at: time + 2))
            camera.sessionStarted(at: time + 2)
        }
        for index in 0...50 { camera.frameReceived(at: 10 + Double(index) * 0.1) }
        XCTAssertNotNil(camera.failed(at: 16))
    }

    func testMissingFramesTriggerRecoveryButPausesAndInterruptionDoNot() {
        var camera = CameraSessionRecovery()
        camera.start(); camera.sessionStarted(at: 100)
        XCTAssertFalse(camera.needsRestart(at: 105))
        XCTAssertTrue(camera.needsRestart(at: 105.1))
        camera.frameReceived(at: 105.2)
        XCTAssertFalse(camera.needsRestart(at: 108.2))
        XCTAssertTrue(camera.needsRestart(at: 108.3))
        camera.beginInterruption()
        XCTAssertFalse(camera.needsRestart(at: 200))
        camera.stop()
        XCTAssertFalse(camera.needsRestart(at: 300))
        XCTAssertFalse(camera.needsRestart(at: .nan))
    }

    func testSparseFramesDoNotResetRetryBudget() throws {
        var camera = CameraSessionRecovery()
        camera.start()
        for time in [0.0, 2.0, 4.0] {
            let retry = try XCTUnwrap(camera.failed(at: time))
            XCTAssertTrue(camera.take(retry, at: time + 2))
            camera.sessionStarted(at: time + 2)
        }
        camera.frameReceived(at: 10); camera.frameReceived(at: 20)
        XCTAssertNil(camera.failed(at: 21))
    }

    func testRepeatedOrPreRestartFrameTimestampsDoNotKeepDeadCaptureAlive() {
        var camera = CameraSessionRecovery()
        camera.start(); camera.sessionStarted(at: 100)
        camera.frameReceived(at: 99)
        XCTAssertTrue(camera.needsRestart(at: 106))
        camera.frameReceived(at: 106)
        for _ in 0..<10 { camera.frameReceived(at: 106) }
        camera.frameReceived(at: 105)
        XCTAssertTrue(camera.needsRestart(at: 110))
    }
}
