import XCTest
@testable import BrakeCheckCore

final class CurveSpeedAdvisorTests: XCTestCase {
    private let origin = NavigationCoordinate(latitude: 33.78, longitude: -84.38)
    private func coordinate(east: Double, north: Double) -> NavigationCoordinate {
        NavigationCoordinate(latitude: origin.latitude + north / 111_320,
                             longitude: origin.longitude + east / (111_320 * cos(origin.latitude * .pi / 180)))
    }
    /// A straight northbound approach, then a quarter circle of the given radius turning east, then straight.
    private func routeWithCurve(radius: Double, approach: Double = 400, exit: Double = 400) -> [NavigationCoordinate] {
        var route: [NavigationCoordinate] = []
        for meters in stride(from: 0.0, through: approach, by: 25) { route.append(coordinate(east: 0, north: meters)) }
        let steps = 24
        for step in 1...steps {
            let angle = Double(step) / Double(steps) * .pi / 2
            route.append(coordinate(east: radius - radius * cos(angle), north: approach + radius * sin(angle)))
        }
        for meters in stride(from: 25.0, through: exit, by: 25) {
            route.append(coordinate(east: radius + meters, north: approach + radius))
        }
        return route
    }
    private func drive(_ advisor: inout CurveSpeedAdvisor, from: Double, to: Double, speed: Double,
                       step: Double = 1) -> [CurveSpeedWarning] {
        var warnings: [CurveSpeedWarning] = []
        var time = 0.0
        var position = from
        while position <= to {
            if let warning = advisor.update(progressFraction: position / advisor.routeLength, speed: speed,
                                            navigating: true, now: time) { warnings.append(warning) }
            position += speed * step; time += step
        }
        return warnings
    }

    func testStraightRouteNeverWarns() {
        var advisor = CurveSpeedAdvisor()
        advisor.setRoute(stride(from: 0.0, through: 1500, by: 25).map { coordinate(east: 0, north: $0) })
        XCTAssertTrue(drive(&advisor, from: 0, to: 1200, speed: 30).isEmpty)
    }

    func testFastApproachToTightCurveWarnsOnceWithAdvisedSpeed() {
        var advisor = CurveSpeedAdvisor()
        advisor.setRoute(routeWithCurve(radius: 50))
        let warnings = drive(&advisor, from: 0, to: 380, speed: 20)
        XCTAssertFalse(warnings.isEmpty)
        let first = warnings[0]
        XCTAssertEqual(first.severity, .sharp)   // 20² / 50 = 8 m/s²
        XCTAssertEqual(first.radius, 50, accuracy: 12)
        XCTAssertEqual(first.advisedSpeed, sqrt(2.5 * first.radius), accuracy: 1e-6)
        XCTAssertGreaterThan(first.distance, 15)
        XCTAssertLessThanOrEqual(first.distance, 20 * 8 + 1)
        XCTAssertEqual(warnings.filter(\.shouldSpeak).count, 1)
        XCTAssertEqual(warnings.first?.shouldSpeak, true)
    }

    func testComfortableSpeedThroughTheSameCurveDoesNotWarn() {
        var advisor = CurveSpeedAdvisor()
        advisor.setRoute(routeWithCurve(radius: 50))
        XCTAssertTrue(drive(&advisor, from: 0, to: 380, speed: 10).isEmpty)   // 10² / 50 = 2 m/s²
        var moderate = CurveSpeedAdvisor()
        moderate.setRoute(routeWithCurve(radius: 120))
        let warnings = drive(&moderate, from: 0, to: 380, speed: 22)          // 22² / 120 ≈ 4.0 m/s²
        XCTAssertEqual(warnings.first?.severity, .caution)
    }

    func testIntersectionTurnsAndSlowSpeedsAreLeftToTheTurnPrompt() {
        var advisor = CurveSpeedAdvisor()
        advisor.setRoute(routeWithCurve(radius: 9))
        XCTAssertTrue(drive(&advisor, from: 0, to: 380, speed: 15).isEmpty)
        var slow = CurveSpeedAdvisor()
        slow.setRoute(routeWithCurve(radius: 50))
        XCTAssertTrue(drive(&slow, from: 0, to: 380, speed: 7).isEmpty)
    }

    func testWarningClearsAfterTheCurveAndRequiresNavigationAndValidInputs() {
        var advisor = CurveSpeedAdvisor()
        advisor.setRoute(routeWithCurve(radius: 50))
        XCTAssertFalse(drive(&advisor, from: 0, to: 380, speed: 20).isEmpty)
        let afterCurve: Double = 400 + 50 * Double.pi / 2 + 60
        XCTAssertNil(advisor.update(progressFraction: afterCurve / advisor.routeLength, speed: 20, navigating: true, now: 100))
        XCTAssertNil(advisor.update(progressFraction: 0.2, speed: 20, navigating: false, now: 101))
        XCTAssertNil(advisor.update(progressFraction: nil, speed: 20, navigating: true, now: 102))
        XCTAssertNil(advisor.update(progressFraction: 1.3, speed: 20, navigating: true, now: 103))
        XCTAssertNil(advisor.update(progressFraction: 0.2, speed: nil, navigating: true, now: 104))
        var empty = CurveSpeedAdvisor()
        empty.setRoute([])
        XCTAssertNil(empty.update(progressFraction: 0.2, speed: 20, navigating: true, now: 105))
    }

    func testHeadingWrapAndIntersectionCornerAreNotRoadCurves() {
        XCTAssertEqual(CurveSpeedAdvisor.wrapped(3.5), 3.5 - 2 * .pi, accuracy: 1e-9)
        XCTAssertEqual(CurveSpeedAdvisor.wrapped(-3.5), -3.5 + 2 * .pi, accuracy: 1e-9)
        XCTAssertEqual(CurveSpeedAdvisor.wrapped(0.4), 0.4, accuracy: 1e-9)
        // A 90° corner with a 15 m radius (arterial right turn) belongs to the turn prompt.
        var advisor = CurveSpeedAdvisor()
        advisor.setRoute(routeWithCurve(radius: 15))
        XCTAssertTrue(drive(&advisor, from: 0, to: 380, speed: 15).isEmpty)
    }
}
