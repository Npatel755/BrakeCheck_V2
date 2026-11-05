import XCTest
@testable import BrakeCheckCore

final class ParkedTripMonitorTests: XCTestCase {
    private func monitor() -> ParkedTripMonitor { ParkedTripMonitor(parkDelay: 480, promptDuration: 90) }

    func testNothingBeforeParkDelay() {
        var m = monitor()
        for second in 0..<480 {
            XCTAssertEqual(m.update(stationary: true, hold: false, at: Double(second)), .none)
        }
    }

    func testPromptCountsDownThenEndsTrip() {
        var m = monitor()
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 0), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 480), .prompt(secondsRemaining: 90))
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 540.5), .prompt(secondsRemaining: 30))
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 569.9), .prompt(secondsRemaining: 1))
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 570), .endTrip)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 571), .none, "The end resets the timer")
    }

    func testMovementResetsEvenDuringPrompt() {
        var m = monitor()
        _ = m.update(stationary: true, hold: false, at: 0)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 500), .prompt(secondsRemaining: 90))
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 520), .prompt(secondsRemaining: 70))
        XCTAssertEqual(m.update(stationary: false, hold: false, at: 521), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 522), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 1001), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 1002), .prompt(secondsRemaining: 90))
    }

    func testUnknownFixNeitherAdvancesNorResets() {
        var m = monitor()
        XCTAssertEqual(m.update(stationary: nil, hold: false, at: 0), .none)
        XCTAssertEqual(m.update(stationary: nil, hold: false, at: 1000), .none, "No stationary evidence yet")
        _ = m.update(stationary: true, hold: false, at: 1000)
        XCTAssertEqual(m.update(stationary: nil, hold: false, at: 1200), .none)
        XCTAssertEqual(m.update(stationary: nil, hold: false, at: 1480), .prompt(secondsRemaining: 90),
                       "A GPS gap after a confirmed stop still counts as parked")
    }

    func testHoldDefersPromptWithoutLosingParkTime() {
        var m = monitor()
        _ = m.update(stationary: true, hold: false, at: 0)
        XCTAssertEqual(m.update(stationary: true, hold: true, at: 600), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 601), .prompt(secondsRemaining: 90),
                       "The countdown starts when the hold lifts, not retroactively")
        XCTAssertEqual(m.update(stationary: true, hold: true, at: 650), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 660), .prompt(secondsRemaining: 90))
    }

    func testKeepDrivingRestartsTheParkDelay() {
        var m = monitor()
        _ = m.update(stationary: true, hold: false, at: 0)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 480), .prompt(secondsRemaining: 90))
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 520), .prompt(secondsRemaining: 50))
        m.keepDriving(at: 520)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 999), .none)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 1000), .prompt(secondsRemaining: 90))
    }

    func testResetClearsEverything() {
        var m = monitor()
        _ = m.update(stationary: true, hold: false, at: 0)
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 480), .prompt(secondsRemaining: 90))
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 500), .prompt(secondsRemaining: 70))
        m.reset()
        XCTAssertEqual(m.update(stationary: true, hold: false, at: 501), .none)
    }

    func testPromptText() {
        XCTAssertEqual(ParkedTripPrompt(secondsRemaining: 42).text, "Parked · trip ends in 42 s")
    }
}
