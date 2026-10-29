import XCTest
@testable import BrakeCheckCore

final class MapOverlayCoalescerTests: XCTestCase {
    func testStationaryNoiseDoesNotContinuouslyReallocateCircle() {
        var budget = MapOverlayCoalescer()
        var allocations = 0
        for frame in 0..<1800 {
            let x = sin(Double(frame)) / 111_195
            if budget.accept(center: NavigationCoordinate(latitude: 0, longitude: x), radius: 10) { allocations += 1 }
        }
        XCTAssertEqual(allocations, 1)
    }
    func testCoalescedCircleStillCoversMovementAndChangingUncertainty() {
        var budget = MapOverlayCoalescer()
        var allocations = 0
        for frame in 0..<1000 {
            let point = NavigationCoordinate(latitude: 0, longitude: Double(frame) / 111_195)
            let radius = 10 + sin(Double(frame))
            if budget.accept(center: point, radius: radius) { allocations += 1 }
            XCTAssertGreaterThanOrEqual(budget.radius + 5,
                                        MapOverlayCoalescer.distance(budget.center!, point) + radius)
        }
        XCTAssertLessThan(allocations, 400)
    }
    func testDatelineNoiseUsesShortDistance() {
        var budget = MapOverlayCoalescer()
        XCTAssertTrue(budget.accept(center: NavigationCoordinate(latitude: 0, longitude: 179.999999), radius: 10))
        XCTAssertFalse(budget.accept(center: NavigationCoordinate(latitude: 0, longitude: -179.999999), radius: 10))
    }
    func testInvalidInputsDoNotPoisonStateAndResetRestoresFirstUpdate() {
        var budget = MapOverlayCoalescer()
        let valid = NavigationCoordinate(latitude: 0, longitude: 0)
        XCTAssertTrue(budget.accept(center: valid, radius: 10))
        XCTAssertFalse(budget.accept(center: NavigationCoordinate(latitude: .nan, longitude: 0), radius: 10))
        XCTAssertFalse(budget.accept(center: valid, radius: .infinity))
        XCTAssertEqual(budget.radius, 10)
        budget.reset()
        XCTAssertTrue(budget.accept(center: valid, radius: 10))
    }
}
