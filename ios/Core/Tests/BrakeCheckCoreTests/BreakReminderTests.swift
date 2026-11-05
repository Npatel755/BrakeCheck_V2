import XCTest
@testable import BrakeCheckCore

final class BreakReminderTests: XCTestCase {
    func testFiresAtTwoHoursThenHourlyOncePerThreshold() {
        var reminder = BreakReminder()
        XCTAssertNil(reminder.update(elapsedSeconds: 7199, moving: true))
        XCTAssertEqual(reminder.update(elapsedSeconds: 7200, moving: true), 2)
        XCTAssertNil(reminder.update(elapsedSeconds: 7201, moving: true))
        XCTAssertNil(reminder.update(elapsedSeconds: 10_799, moving: true))
        XCTAssertEqual(reminder.update(elapsedSeconds: 10_800, moving: true), 3)
        XCTAssertNil(reminder.update(elapsedSeconds: 12_000, moving: true))
    }

    func testWaitsForMovementAndSkipsThresholdsPassedWhileParked() {
        var reminder = BreakReminder()
        XCTAssertNil(reminder.update(elapsedSeconds: 7200, moving: false))
        XCTAssertNil(reminder.update(elapsedSeconds: 9000, moving: false))
        XCTAssertEqual(reminder.update(elapsedSeconds: 11_000, moving: true), 3, "one reminder for the elapsed time, not a backlog")
        XCTAssertNil(reminder.update(elapsedSeconds: 11_001, moving: true))
        XCTAssertEqual(reminder.update(elapsedSeconds: 14_400, moving: true), 4)
    }

    func testResetAndInvalidInput() {
        var reminder = BreakReminder()
        XCTAssertEqual(reminder.update(elapsedSeconds: 7200, moving: true), 2)
        reminder.reset()
        XCTAssertEqual(reminder.update(elapsedSeconds: 7200, moving: true), 2)
        XCTAssertNil(reminder.update(elapsedSeconds: .nan, moving: true))
        XCTAssertNil(reminder.update(elapsedSeconds: .infinity, moving: true))
        XCTAssertEqual(BreakReminder.text(hours: 2), "2 hours on the road · take a break when you can")
    }
}
