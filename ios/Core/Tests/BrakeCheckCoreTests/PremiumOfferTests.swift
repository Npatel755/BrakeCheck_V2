import XCTest
@testable import BrakeCheckCore

final class PremiumOfferTests: XCTestCase {
    private var calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        calendar.firstWeekday = 1
        return calendar
    }()

    private func date(_ text: String) -> Date {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = calendar.timeZone
        return formatter.date(from: text)!
    }

    private func trip(_ start: String, minutes: Double = 30, brakes: Int = 0, closeCalls: Int = 0) -> TripMetrics {
        let begin = date(start)
        return TripMetrics(startedAt: begin, endedAt: begin.addingTimeInterval(minutes * 60), maxSpeedMPH: 40,
                           estimatedLeadHardBrakes: brakes, estimatedCloseCalls: closeCalls)
    }

    func testProductIDsRoundTripAndOnlyLifetimeIsOneTime() {
        for plan in PremiumPlan.allCases {
            XCTAssertEqual(PremiumPlan(productID: plan.productID), plan)
            XCTAssertTrue(plan.productID.hasPrefix("com.brakesense.premium."))
        }
        XCTAssertNil(PremiumPlan(productID: "com.brakecheck.prototype.premium.monthly"))
        XCTAssertEqual(PremiumPlan.allCases.filter { !$0.isSubscription }, [.lifetime])
    }

    func testLaunchPricesGiveAboutTwoFiftyAMonthAndHalfOff() {
        XCTAssertEqual(PremiumPricing.monthlyEquivalent(annual: Decimal(string: "29.99")!), Decimal(string: "2.50")!)
        XCTAssertEqual(PremiumPricing.annualSavingsPercent(annual: Decimal(string: "29.99")!,
                                                           monthly: Decimal(string: "4.99")!), 50)
    }

    func testNoSavingLineWhenYearlyIsNotCheaper() {
        XCTAssertNil(PremiumPricing.annualSavingsPercent(annual: 60, monthly: 5))
        XCTAssertNil(PremiumPricing.annualSavingsPercent(annual: 80, monthly: 5))
        XCTAssertNil(PremiumPricing.annualSavingsPercent(annual: 0, monthly: 5))
        XCTAssertNil(PremiumPricing.annualSavingsPercent(annual: 30, monthly: 0))
    }

    func testTrialReminderIsDayFiveOfSevenAndChargeIsDaySeven() {
        let start = date("2026-11-16T09:00:00-05:00")
        XCTAssertEqual(TrialReminder.fireDate(trialStart: start, trialDays: 7, calendar: calendar),
                       date("2026-11-21T09:00:00-05:00"))
        XCTAssertEqual(TrialReminder.chargeDate(trialStart: start, trialDays: 7, calendar: calendar),
                       date("2026-11-23T09:00:00-05:00"))
        XCTAssertNil(TrialReminder.fireDate(trialStart: start, trialDays: 2, calendar: calendar))
        XCTAssertNil(TrialReminder.chargeDate(trialStart: start, trialDays: 0, calendar: calendar))
    }

    func testFreeHistoryShowsTheLastWeekAndLocksOlderTripsWithoutDroppingThem() {
        let now = date("2026-11-20T12:00:00-05:00")
        let trips = [trip("2026-11-19T08:00:00-05:00"), trip("2026-11-14T08:00:00-05:00"),
                     trip("2026-11-12T08:00:00-05:00"), trip("2026-10-01T08:00:00-04:00")]
        let free = TripHistoryPolicy.split(trips, now: now, premium: false, calendar: calendar)
        XCTAssertEqual(free.visible.map(\.id), trips.prefix(2).map(\.id))
        XCTAssertEqual(free.lockedCount, 2)
        let premium = TripHistoryPolicy.split(trips, now: now, premium: true, calendar: calendar)
        XCTAssertEqual(premium.visible.count, 4)
        XCTAssertEqual(premium.lockedCount, 0)
    }

    func testWeeklyTrendsCoverEmptyWeeksNewestFirst() {
        let now = date("2026-11-20T12:00:00-05:00")   // Friday; the week starts Sunday Nov 15
        let trips = [trip("2026-11-19T08:00:00-05:00", minutes: 60, brakes: 3, closeCalls: 1),
                     trip("2026-11-16T08:00:00-05:00", minutes: 60, brakes: 1),
                     trip("2026-11-03T08:00:00-05:00", minutes: 30, brakes: 2)]
        let weeks = TripTrends.weekly(trips, now: now, weeks: 4, calendar: calendar)
        XCTAssertEqual(weeks.count, 4)
        XCTAssertEqual(weeks[0].weekStart, date("2026-11-15T00:00:00-05:00"))
        XCTAssertEqual(weeks.map(\.trips), [2, 0, 1, 0])
        XCTAssertEqual(weeks[0].hardBrakes, 4)
        XCTAssertEqual(weeks[0].closeCalls, 1)
        XCTAssertEqual(weeks[0].hardBrakesPerHour ?? -1, 2, accuracy: 0.0001)
        XCTAssertNil(weeks[1].hardBrakesPerHour)
        XCTAssertEqual(weeks[2].hardBrakesPerHour ?? -1, 4, accuracy: 0.0001)
    }
}
