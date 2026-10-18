import XCTest
@testable import BrakeCheckCore

final class PlusPolicyTests: XCTestCase {
    func testFreeUsersSeeTheMostRecentTripsOnly() {
        XCTAssertEqual(PlusPolicy.visibleTripCount(total: 3, isPlus: false), 3)
        XCTAssertEqual(PlusPolicy.visibleTripCount(total: 12, isPlus: false), PlusPolicy.freeTripHistoryCount)
        XCTAssertEqual(PlusPolicy.lockedTripCount(total: 12, isPlus: false), 12 - PlusPolicy.freeTripHistoryCount)
    }

    func testPlusSeesEveryTrip() {
        XCTAssertEqual(PlusPolicy.visibleTripCount(total: 12, isPlus: true), 12)
        XCTAssertEqual(PlusPolicy.lockedTripCount(total: 12, isPlus: true), 0)
    }

    func testEmptyOrBadCountsNeverGoNegative() {
        XCTAssertEqual(PlusPolicy.visibleTripCount(total: 0, isPlus: false), 0)
        XCTAssertEqual(PlusPolicy.lockedTripCount(total: -1, isPlus: false), 0)
    }

    func testTrialReminderComesTwoDaysBeforeTheCharge() {
        XCTAssertEqual(PlusPolicy.trialReminderDay(trialDays: 7), 5)
        XCTAssertEqual(PlusPolicy.trialReminderDay(trialDays: 3), 1)
        XCTAssertNil(PlusPolicy.trialReminderDay(trialDays: 2))
    }

    func testAnnualSavingsAgainstMonthly() {
        // $3.99/mo vs $24.99/yr: 12 × 3.99 = 47.88, saving 22.89 → 47%.
        XCTAssertEqual(PlusPolicy.annualSavingsPercent(monthly: Decimal(string: "3.99")!, annual: Decimal(string: "24.99")!), 47)
        XCTAssertNil(PlusPolicy.annualSavingsPercent(monthly: 2, annual: 30))
        XCTAssertNil(PlusPolicy.annualSavingsPercent(monthly: 0, annual: 10))
    }
}
