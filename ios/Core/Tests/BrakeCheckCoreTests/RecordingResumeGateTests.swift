import XCTest
@testable import BrakeCheckCore

final class RecordingResumeGateTests: XCTestCase {
    func testRepeatedResumeTapsShareOneStorageCheck() throws {
        var gate = RecordingResumeGate()
        let first = try XCTUnwrap(gate.begin())
        XCTAssertNil(gate.begin()); XCTAssertNil(gate.begin())
        XCTAssertEqual(gate.pending, first)
        XCTAssertTrue(gate.take(first)); XCTAssertNil(gate.pending)
        XCTAssertFalse(gate.take(first))
    }
    func testStopPauseOrNewStartInvalidatesOldStorageCompletion() throws {
        var gate = RecordingResumeGate()
        let beforeLifecycle = try XCTUnwrap(gate.begin())
        gate.invalidate()
        XCTAssertFalse(gate.take(beforeLifecycle))
        XCTAssertNil(gate.pending)
    }
    func testLateCompletionCannotConsumeNewerResumeRequest() throws {
        var gate = RecordingResumeGate()
        let old = try XCTUnwrap(gate.begin())
        gate.invalidate()
        let current = try XCTUnwrap(gate.begin())
        XCTAssertNotEqual(old, current)
        XCTAssertFalse(gate.take(old))
        XCTAssertEqual(gate.pending, current)
        XCTAssertTrue(gate.take(current))
    }
    func testFailedStorageCheckCanBeRetriedWithFreshRequest() throws {
        var gate = RecordingResumeGate()
        let failed = try XCTUnwrap(gate.begin())
        XCTAssertTrue(gate.take(failed)) // fresh storage result still says full
        let retry = try XCTUnwrap(gate.begin())
        XCTAssertNotEqual(retry, failed)
        XCTAssertTrue(gate.take(retry))
    }
}
