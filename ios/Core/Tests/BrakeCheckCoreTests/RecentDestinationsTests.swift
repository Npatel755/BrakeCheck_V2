import XCTest
@testable import BrakeCheckCore

final class RecentDestinationsTests: XCTestCase {
    private func place(_ index: Int, time: Double? = nil) -> RecentDestination {
        RecentDestination(title: "Place \(index)", subtitle: "Street", latitude: Double(index), longitude: 0,
                          selectedAt: Date(timeIntervalSince1970: time ?? Double(index)))
    }
    func testHistoryIsBoundedAndNewestFirst() {
        let history = RecentDestinations.normalized((0..<30).map { place($0) })
        XCTAssertEqual(history.count, 20)
        XCTAssertEqual(history.first?.title, "Place 29")
        XCTAssertEqual(history.last?.title, "Place 10")
    }
    func testChoosingSamePlaceMovesItToTopWithoutDuplicates() {
        let history = [place(1), place(2)]
        let chosen = place(1, time: 10)
        let result = RecentDestinations.adding(chosen, to: history)
        XCTAssertEqual(result.count, 2)
        XCTAssertEqual(result.first?.id, chosen.id)
    }
    func testInvalidCoordinatesAndEmptyTitlesAreNotSaved() {
        let invalid = [RecentDestination(title: "", subtitle: "", latitude: 1, longitude: 1),
                       RecentDestination(title: "Bad", subtitle: "", latitude: 91, longitude: 0),
                       RecentDestination(title: "Bad", subtitle: "", latitude: .nan, longitude: 0)]
        XCTAssertTrue(RecentDestinations.normalized(invalid).isEmpty)
        XCTAssertEqual(RecentDestinations.adding(invalid[0], to: [place(1)]).count, 1)
    }
    func testHistoryCanRoundTripThroughPersistence() throws {
        let original = [place(1), place(2)]
        let restored = try JSONDecoder().decode([RecentDestination].self, from: JSONEncoder().encode(original))
        XCTAssertEqual(restored.map(\.id), original.map(\.id))
        XCTAssertEqual(restored.map(\.latitude), original.map(\.latitude))
        XCTAssertEqual(restored.map(\.title), original.map(\.title))
    }
}
