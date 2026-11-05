import XCTest
@testable import BrakeCheckCore

final class TripSummarySpeechTests: XCTestCase {
    private func trip(minutes: Double, hardBrakes: Int = 0) -> TripMetrics {
        let start = Date(timeIntervalSince1970: 0)
        return TripMetrics(startedAt: start, endedAt: start.addingTimeInterval(minutes * 60), maxSpeedMPH: nil,
                           estimatedLeadHardBrakes: hardBrakes, estimatedCloseCalls: 0)
    }

    func testEnglishAndSpanishWithScore() {
        XCTAssertEqual(TripSummarySpeech.text(for: trip(minutes: 24), language: "en"), "Trip saved. 24 minutes. Smooth, 100.")
        XCTAssertEqual(TripSummarySpeech.text(for: trip(minutes: 20, hardBrakes: 6), language: "es"),
                       "Viaje guardado. 20 minutos. Movido, 64.")
    }

    func testShortTripsHaveNoScoreAndAtLeastOneMinute() {
        XCTAssertEqual(TripSummarySpeech.text(for: trip(minutes: 0.2), language: "en"), "Trip saved. 1 minute.")
        XCTAssertEqual(TripSummarySpeech.text(for: trip(minutes: 1.4), language: "es"), "Viaje guardado. 1 minuto.")
    }
}
