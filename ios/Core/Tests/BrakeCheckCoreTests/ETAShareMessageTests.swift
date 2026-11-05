import XCTest
@testable import BrakeCheckCore

final class ETAShareMessageTests: XCTestCase {
    private let noon = Date(timeIntervalSince1970: 1_700_000_000) // 2023-11-14 22:13:20 UTC
    private let locale = Locale(identifier: "en_US_POSIX")
    private let utc = TimeZone(identifier: "UTC")!

    func testNamesDestinationArrivalAndDuration() {
        XCTAssertEqual(ETAShareMessage.text(destination: " Piedmont Park ", remainingSeconds: 18 * 60, now: noon,
                                            locale: locale, timeZone: utc),
                       "On my way to Piedmont Park — arriving around 10:31 PM (18 min). Sent from BrakeSense.")
        XCTAssertEqual(ETAShareMessage.text(destination: nil, remainingSeconds: 95 * 60, now: noon, locale: locale, timeZone: utc),
                       "On my way — arriving around 11:48 PM (1 h 35 min). Sent from BrakeSense.")
    }

    func testRejectsInvalidRemainingTimeAndFloorsAtOneMinute() {
        XCTAssertNil(ETAShareMessage.text(destination: "Home", remainingSeconds: .nan, now: noon, locale: locale, timeZone: utc))
        XCTAssertNil(ETAShareMessage.text(destination: "Home", remainingSeconds: -1, now: noon, locale: locale, timeZone: utc))
        XCTAssertEqual(ETAShareMessage.text(destination: "", remainingSeconds: 10, now: noon, locale: locale, timeZone: utc),
                       "On my way — arriving around 10:13 PM (1 min). Sent from BrakeSense.")
    }

    func testLaneAlertKindsDecodeAndAreNotCollisionEvidence() throws {
        let data = Data(#"["laneDeparture","cutIn","hardBrake"]"#.utf8)
        let kinds = try JSONDecoder().decode([TripRoute.Kind].self, from: data)
        XCTAssertEqual(kinds.map(\.isLaneAlert), [true, true, false])
        XCTAssertEqual(kinds[0].title, "Drifted toward the lane line")
    }
}
