import XCTest
@testable import BrakeCheckCore

final class N37CalibrationLockTests: XCTestCase {
    private let pose = CameraCalibrationEstimate(horizon: 0.7, yawDegrees: 2, pitchDegrees: 8, confidence: 0.9)
    func testMissingDetectionsDoNotUnlockSavedOrientation() {
        var lock = CameraOrientationLock()
        XCTAssertEqual(lock.update(pose, signature: "mount-A", observedAt: 10), pose)
        for time in [10.2, 12, 60, 1000] {
            XCTAssertEqual(lock.update(nil, signature: "mount-A", observedAt: time), pose)
        }
    }
    func testChangedRoadGeometryCannotRewriteTheMount() {
        var lock = CameraOrientationLock()
        _ = lock.update(pose, signature: "mount-A", observedAt: 10)
        let other = CameraCalibrationEstimate(horizon: 0.82, yawDegrees: 12, pitchDegrees: 14, confidence: 0.9)
        for index in 0..<20 {
            XCTAssertEqual(lock.update(other, signature: "mount-A", observedAt: 11 + Double(index) * 0.2), pose)
        }
        XCTAssertFalse(CameraOrientationLock.agrees(other, pose))
    }
    func testCameraOrHeightChangeRequiresANewPose() {
        var lock = CameraOrientationLock()
        _ = lock.update(pose, signature: "mount-A", observedAt: 10)
        XCTAssertNil(lock.update(nil, signature: "mount-B", observedAt: 11))
        XCTAssertNil(lock.record)
        XCTAssertEqual(lock.update(pose, signature: "mount-B", observedAt: 12), pose)
        XCTAssertEqual(lock.record?.signature, "mount-B")
    }
    func testStartupWithUnknownGeometryDoesNotDestroySavedConfiguration() {
        var lock = CameraOrientationLock()
        _ = lock.update(pose, signature: "mount-A", observedAt: 10)
        XCTAssertNil(lock.update(nil, signature: "unavailable", observedAt: 12))
        XCTAssertNotNil(lock.record)
        XCTAssertEqual(lock.update(nil, signature: "mount-A", observedAt: 14), pose)
    }
    func testSavedPoseRestoresAcrossAppRelaunch() throws {
        var lock = CameraOrientationLock()
        _ = lock.update(pose, signature: "mount-A", observedAt: 10)
        let data = try JSONEncoder().encode(XCTUnwrap(lock.record))
        let record = try JSONDecoder().decode(CameraOrientationRecord.self, from: data)
        var restored = CameraOrientationLock(restored: record)
        XCTAssertEqual(restored.update(nil, signature: "mount-A", observedAt: 100), pose)
        restored.reset()
        XCTAssertNil(restored.record)
        XCTAssertNil(restored.update(nil, signature: "mount-A", observedAt: 101))
    }
    func testInvalidPoseCannotEstablishOrRestoreLock() {
        let weak = CameraCalibrationEstimate(horizon: 0.7, yawDegrees: 2, pitchDegrees: 8, confidence: 0.3)
        var lock = CameraOrientationLock()
        XCTAssertNil(lock.update(weak, signature: "mount-A", observedAt: 10))
        XCTAssertNil(lock.update(pose, signature: "", observedAt: 10))
        XCTAssertNil(lock.update(pose, signature: "mount-A", observedAt: .nan))
        XCTAssertNil(CameraOrientationLock(restored: CameraOrientationRecord(signature: "mount-A", estimate: weak)).record)
    }
    func testSavedConfigurationCannotAuthorizeMissingLiveRoadTracking() {
        var lock = CameraOrientationLock()
        _ = lock.update(pose, signature: "mount-A", observedAt: 10)
        XCTAssertNotNil(lock.update(nil, signature: "mount-A", observedAt: 100))
        XCTAssertEqual(CameraARConfidencePolicy.level(scene: nil, verified: true, routeMatched: true,
            navigationReliable: true, branchAmbiguous: false, at: 100), .lost)
    }
    func testOnlyFreshTrustworthyMovementVetoesParkedAcknowledgement() {
        func sample(_ speed: Double, _ accuracy: Double, simulated: Bool = false) -> GPSSpeedSample {
            GPSSpeedSample(metersPerSecond: speed, accuracy: accuracy, acceleration: nil, measurementUptime: 10, simulated: simulated)
        }
        XCTAssertTrue(sample(6, 0.5).isConfidentlyMoving(at: 10.5))
        XCTAssertFalse(sample(0, 0.5).isConfidentlyMoving(at: 10.5))
        XCTAssertFalse(sample(2, 2).isConfidentlyMoving(at: 10.5))
        XCTAssertFalse(sample(6, 0.5).isConfidentlyMoving(at: 12))
        XCTAssertFalse(sample(6, 0.5).isConfidentlyMoving(at: 9))
        XCTAssertFalse(sample(6, 0.5, simulated: true).isConfidentlyMoving(at: 10.5))
    }
}
