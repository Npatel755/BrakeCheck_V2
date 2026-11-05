import XCTest
@testable import BrakeCheckCore

final class LaneLevelGuidanceTests: XCTestCase {
    private typealias Point = LanePredictionObservation.Point
    private typealias Boundary = LanePredictionObservation.Boundary

    private func line(slot: Int, x: Double, from: Double = 0.05, to: Double = 0.45) -> Boundary {
        Boundary(laneSlot: slot, confidence: 0.9, points: stride(from: from, through: to, by: 0.05).map { Point(x: x, y: $0) })
    }
    private func dashed(slot: Int, x: Double) -> [Boundary] {
        [line(slot: slot, x: x, from: 0.05, to: 0.15), line(slot: slot, x: x, from: 0.3, to: 0.45)]
    }
    private func observation(_ boundaries: [Boundary], at time: TimeInterval) -> LanePredictionObservation {
        LanePredictionObservation(capturedAt: time, boundaries: boundaries)
    }
    private var threeLaneRightDashed: [Boundary] {
        [line(slot: 0, x: 0.1), line(slot: 1, x: 0.35)] + dashed(slot: 2, x: 0.65) + [line(slot: 3, x: 0.9)]
    }

    func testRightTurnWithDashedRightBoundaryAdvisesOneLaneRightOnceStable() {
        var engine = LaneLevelGuidance()
        var advices: [LaneGuidanceAdvice] = []
        for step in 0..<6 {
            let time = 100 + Double(step) * 0.3
            if let advice = engine.update(observation(threeLaneRightDashed, at: time), maneuver: .right,
                maneuverKey: "Turn right onto Peachtree St", distance: 300, speed: 15, navigating: true, now: time + 0.05) {
                advices.append(advice)
            }
        }
        // Three observations over 0.8 s are required before the first advice.
        XCTAssertEqual(advices.count, 3)
        XCTAssertEqual(advices.first?.direction, .right)
        XCTAssertEqual(advices.first?.lanesVisible, 3)
        XCTAssertEqual(advices.first?.laneIndex, 1)
        XCTAssertEqual(advices.first?.text, "Move one lane right for the turn, when safe")
        XCTAssertEqual(advices.map(\.shouldSpeak), [true, false, false])
        XCTAssertNotNil(advices.first?.spoken)
    }

    func testSolidRightBoundaryOrNoRightLaneMeansStayInLane() {
        for boundaries in [
            [line(slot: 0, x: 0.1), line(slot: 1, x: 0.35), line(slot: 2, x: 0.65), line(slot: 3, x: 0.9)],
            [line(slot: 0, x: 0.1), line(slot: 1, x: 0.35)] + dashed(slot: 2, x: 0.65)
        ] {
            var engine = LaneLevelGuidance()
            var last: LaneGuidanceAdvice?
            for step in 0..<5 {
                let time = Double(step) * 0.3
                last = engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "Turn right",
                    distance: 200, speed: 12, navigating: true, now: time) ?? last
            }
            XCTAssertEqual(last?.direction, .stay)
            XCTAssertEqual(last?.text, "Stay in this lane for the turn")
            XCTAssertNil(last?.spoken)
            XCTAssertEqual(last?.shouldSpeak, false)
        }
    }

    func testLeftMoveIsNeverAdvisedAcrossAContinuousCentreLine() {
        // Two-way road: a far-left boundary exists (the oncoming lane's edge) but the
        // centre line beside the vehicle is solid, so the advice must be to stay.
        let twoWay = [line(slot: 0, x: 0.1), line(slot: 1, x: 0.35)] + dashed(slot: 2, x: 0.65) + [line(slot: 3, x: 0.9)]
        var engine = LaneLevelGuidance()
        var last: LaneGuidanceAdvice?
        for step in 0..<5 {
            let time = Double(step) * 0.3
            last = engine.update(observation(twoWay, at: time), maneuver: .left, maneuverKey: "Turn left",
                distance: 200, speed: 12, navigating: true, now: time) ?? last
        }
        XCTAssertEqual(last?.direction, .stay)
        let dashedCentre = [line(slot: 0, x: 0.1)] + dashed(slot: 1, x: 0.35) + [line(slot: 2, x: 0.65)]
        engine.reset(); last = nil
        for step in 0..<5 {
            let time = Double(step) * 0.3
            last = engine.update(observation(dashedCentre, at: time), maneuver: .left, maneuverKey: "Turn left",
                distance: 200, speed: 12, navigating: true, now: time) ?? last
        }
        XCTAssertEqual(last?.direction, .left)
        XCTAssertEqual(last?.lanesVisible, 2)
        XCTAssertEqual(last?.laneIndex, 1)
    }

    func testGatesRequireNavigationTurnDistanceSpeedAndFreshObservation() {
        var engine = LaneLevelGuidance()
        let boundaries = threeLaneRightDashed
        for step in 0..<5 {
            let time = Double(step) * 0.3
            XCTAssertNil(engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "k",
                distance: 300, speed: 15, navigating: false, now: time))
            XCTAssertNil(engine.update(observation(boundaries, at: time), maneuver: .straight, maneuverKey: "k",
                distance: 300, speed: 15, navigating: true, now: time))
            XCTAssertNil(engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "k",
                distance: 40, speed: 15, navigating: true, now: time))
            // 25 s of travel at the current speed bounds how early the advice may appear.
            XCTAssertNil(engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "k",
                distance: 400, speed: 15, navigating: true, now: time))
            XCTAssertNil(engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "k",
                distance: 300, speed: 2, navigating: true, now: time))
            XCTAssertNil(engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "k",
                distance: 300, speed: 15, navigating: true, now: time + 1.0))
            XCTAssertNil(engine.update(nil, maneuver: .right, maneuverKey: "k",
                distance: 300, speed: 15, navigating: true, now: time))
        }
        // Each rejected update also cleared the stability history.
        XCTAssertNil(engine.update(observation(boundaries, at: 10), maneuver: .right, maneuverKey: "k",
            distance: 300, speed: 15, navigating: true, now: 10))
    }

    func testObservationGapRestartsStabilityAndNewManeuverSpeaksAgain() {
        var engine = LaneLevelGuidance()
        let boundaries = threeLaneRightDashed
        for step in 0..<3 {
            let time = Double(step) * 0.3
            _ = engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "first",
                distance: 300, speed: 15, navigating: true, now: time)
        }
        // A 0.9 s gap in lane observations means the previous readings no longer count.
        XCTAssertNil(engine.update(observation(boundaries, at: 1.5), maneuver: .right, maneuverKey: "first",
            distance: 300, speed: 15, navigating: true, now: 1.5))
        var spoken = 0
        for step in 1...4 {
            let time = 1.5 + Double(step) * 0.3
            if engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "first",
                distance: 250, speed: 15, navigating: true, now: time)?.shouldSpeak == true { spoken += 1 }
        }
        XCTAssertEqual(spoken, 1)
        for step in 5...9 {
            let time = 1.5 + Double(step) * 0.3
            if engine.update(observation(boundaries, at: time), maneuver: .right, maneuverKey: "second",
                distance: 300, speed: 15, navigating: true, now: time)?.shouldSpeak == true { spoken += 1 }
        }
        XCTAssertEqual(spoken, 2)
    }

    func testManeuverDirectionFollowsTheInstructionNotTheRoadName() {
        XCTAssertEqual(LaneGuidanceManeuver(instruction: "Turn right onto Left Fork Rd"), .right)
        XCTAssertEqual(LaneGuidanceManeuver(instruction: "Keep left to take exit 250"), .left)
        XCTAssertEqual(LaneGuidanceManeuver(instruction: "Make a U-turn"), .left)
        XCTAssertEqual(LaneGuidanceManeuver(instruction: "Continue on Right St"), .straight)
        XCTAssertEqual(LaneGuidanceManeuver(instruction: "Merge onto I-85 N"), .straight)
    }
}
