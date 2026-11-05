import XCTest
@testable import BrakeCheckCore

final class TripDayDigestTests: XCTestCase {
    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }
    private func trip(startHour: Double, minutes: Double, day: Double = 0, hardBrakes: Int = 0) -> TripMetrics {
        let start = Date(timeIntervalSince1970: (day * 24 + startHour) * 3600)
        return TripMetrics(startedAt: start, endedAt: start.addingTimeInterval(minutes * 60), maxSpeedMPH: nil,
                           estimatedLeadHardBrakes: hardBrakes, estimatedCloseCalls: 0)
    }

    func testSumsTodayOnlyAndAveragesScoredTrips() {
        let now = Date(timeIntervalSince1970: 20 * 3600)
        let trips = [trip(startHour: 8, minutes: 30), trip(startHour: 12, minutes: 50, hardBrakes: 5),
                     trip(startHour: 18, minutes: 1), trip(startHour: 9, minutes: 40, day: -1)]
        let digest = TripDayDigest(trips, now: now, calendar: calendar)
        XCTAssertEqual(digest?.trips, 3)
        XCTAssertEqual(digest?.drivingSeconds, 81 * 60)
        XCTAssertEqual(digest?.averageScore, 94)
        XCTAssertEqual(digest?.line, "3 trips · 1 h 21 min · score 94")
    }

    func testNeedsTwoTripsAndToleratesUnscored() {
        let now = Date(timeIntervalSince1970: 20 * 3600)
        XCTAssertNil(TripDayDigest([trip(startHour: 8, minutes: 30)], now: now, calendar: calendar))
        XCTAssertNil(TripDayDigest([], now: now, calendar: calendar))
        let short = TripDayDigest([trip(startHour: 8, minutes: 1), trip(startHour: 9, minutes: 1)], now: now, calendar: calendar)
        XCTAssertNil(short?.averageScore)
        XCTAssertEqual(short?.line, "2 trips · 2 min")
    }
}
