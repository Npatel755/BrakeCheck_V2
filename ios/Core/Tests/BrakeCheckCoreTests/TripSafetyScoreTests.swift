import XCTest
@testable import BrakeCheckCore

final class TripSafetyScoreTests: XCTestCase {
    func testCleanTripScoresFull() {
        let score = TripSafetyScore(durationSeconds: 30 * 60, hardBrakes: 0, closeCalls: 0)
        XCTAssertEqual(score?.value, 100)
        XCTAssertEqual(score?.label, "Smooth")
    }

    func testShortOrInvalidTripsHaveNoScore() {
        XCTAssertNil(TripSafetyScore(durationSeconds: 90, hardBrakes: 0, closeCalls: 0))
        XCTAssertNil(TripSafetyScore(durationSeconds: .nan, hardBrakes: 0, closeCalls: 0))
        XCTAssertNil(TripSafetyScore(durationSeconds: 600, hardBrakes: -1, closeCalls: 0))
    }

    func testEventsLowerScorePerTenMinutes() {
        // 2 hard brakes + 1 close call over 20 min → 4 weighted / 2 blocks = 2 per block → 76.
        let score = TripSafetyScore(durationSeconds: 20 * 60, hardBrakes: 2, closeCalls: 1)
        XCTAssertEqual(score?.value, 76)
        XCTAssertEqual(score?.label, "Steady")
    }

    func testCloseCallsWeighMoreThanHardBrakes() {
        let brakes = TripSafetyScore(durationSeconds: 600, hardBrakes: 2, closeCalls: 0)!
        let close = TripSafetyScore(durationSeconds: 600, hardBrakes: 0, closeCalls: 2)!
        XCTAssertGreaterThan(brakes.value, close.value)
    }

    func testShortTripsAreNotExaggeratedAndFloorIsZero() {
        // 3 min counts as half a block, not 0.3, so one hard brake costs 24 not 40.
        XCTAssertEqual(TripSafetyScore(durationSeconds: 180, hardBrakes: 1, closeCalls: 0)?.value, 76)
        XCTAssertEqual(TripSafetyScore(durationSeconds: 600, hardBrakes: 20, closeCalls: 20)?.value, 0)
        XCTAssertEqual(TripSafetyScore(durationSeconds: 600, hardBrakes: 20, closeCalls: 20)?.label, "Rough")
    }

    func testTripScoreCountsOwnAndLeadHardBrakesAlike() {
        let start = Date(timeIntervalSince1970: 0), end = Date(timeIntervalSince1970: 1200)
        let lead = TripMetrics(startedAt: start, endedAt: end, maxSpeedMPH: nil, estimatedLeadHardBrakes: 2, estimatedCloseCalls: 0)
        let own = TripMetrics(startedAt: start, endedAt: end, maxSpeedMPH: nil, estimatedLeadHardBrakes: 0,
                              estimatedCloseCalls: 0, ownHardBrakes: 2)
        let both = TripMetrics(startedAt: start, endedAt: end, maxSpeedMPH: nil, estimatedLeadHardBrakes: 1,
                               estimatedCloseCalls: 0, ownHardBrakes: 1)
        XCTAssertEqual(TripSafetyScore(lead)?.value, 88)
        XCTAssertEqual(TripSafetyScore(own)?.value, 88)
        XCTAssertEqual(TripSafetyScore(both)?.value, 88)
        XCTAssertEqual(both.totalHardBrakes, 2)
        XCTAssertNil(TripSafetyScore(TripMetrics(startedAt: start, endedAt: start.addingTimeInterval(60),
                                                 maxSpeedMPH: nil, estimatedLeadHardBrakes: 0, estimatedCloseCalls: 0)))
    }
}
