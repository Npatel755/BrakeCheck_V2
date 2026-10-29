import XCTest
@testable import BrakeCheckCore

final class NavigationVoiceTests: XCTestCase {
    func testSilentProviderStepsDoNotAnnounceTheTurnAtTheWrongBoundary() throws {
        let steps = [100.0, 50.0, 80.0, 70.0]
        XCTAssertEqual(NavigationVoiceGuidance.distanceToManeuver(projectedStep: 1, instructionStep: 3,
            distanceToStep: 10, stepDistances: steps, routeDistance: 300), 140)
        XCTAssertEqual(NavigationVoiceGuidance.distanceToManeuver(projectedStep: 0, instructionStep: 1,
            distanceToStep: 100, stepDistances: steps, routeDistance: 300), 100)
        XCTAssertEqual(NavigationVoiceGuidance.distanceToManeuver(projectedStep: 1, instructionStep: 2,
            distanceToStep: 20, stepDistances: steps, routeDistance: 600), 120)
        XCTAssertEqual(NavigationVoiceGuidance.distanceToManeuver(projectedStep: 2, instructionStep: 2,
            distanceToStep: 20, stepDistances: steps, routeDistance: 300), 20)
    }

    func testInvalidManeuverDistanceInputsAreRejected() {
        for steps in [[Double.nan], [-1.0], [Double.infinity], [0.0, 0.0], []] {
            XCTAssertNil(NavigationVoiceGuidance.distanceToManeuver(projectedStep: 0, instructionStep: 1,
                distanceToStep: 10, stepDistances: steps, routeDistance: 100))
        }
        XCTAssertNil(NavigationVoiceGuidance.distanceToManeuver(projectedStep: 2, instructionStep: 1,
            distanceToStep: 10, stepDistances: [50, 50], routeDistance: 100))
    }

    func testHighwayRemindersAllowTimeToHearDirectionsBeforeTheTurn() {
        // At 30 m/s the old 100 m gate gave only 3.3 seconds.
        XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 440, speed: 30), 1)
        XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 190, speed: 30), 2)
        XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 50, speed: 30), 2)
        XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 15, speed: 30), 3)
        XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 601, speed: 60), 0)
    }

    func testSlowAndUnknownSpeedRetainBoundedPrompts() {
        for speed: Double? in [nil, -1, .nan, .infinity, 66, 0] {
            XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 121, speed: speed), 0)
            XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 120, speed: speed), 1)
            XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 40, speed: speed), 2)
            XCTAssertEqual(NavigationVoiceGuidance.stage(distance: 0, speed: speed), 3)
        }
        for distance: Double in [-1, .nan, .infinity] {
            XCTAssertNil(NavigationVoiceGuidance.stage(distance: distance, speed: 10))
        }
    }

    func testInterruptedInstructionIsRepeatedFromTheLatestPosition() throws {
        var progress = NavigationVoiceProgress()
        let interrupted = try XCTUnwrap(progress.begin(step: 1, stage: 1))
        XCTAssertFalse(progress.shouldAnnounce(step: 1, stage: 1))
        progress.cancel()
        XCTAssertTrue(progress.shouldAnnounce(step: 1, stage: 1))
        let urgent = try XCTUnwrap(progress.begin(step: 1, stage: 3))
        progress.finish(interrupted) // A late callback cannot consume the new prompt.
        XCTAssertFalse(progress.shouldAnnounce(step: 1, stage: 3))
        progress.finish(urgent)
        for stage in 0...3 { XCTAssertFalse(progress.shouldAnnounce(step: 1, stage: stage)) }
        XCTAssertTrue(progress.shouldAnnounce(step: 2, stage: 0))
    }

    func testCompletedReminderDoesNotHideAnInterruptedLaterStage() throws {
        var progress = NavigationVoiceProgress()
        let first = try XCTUnwrap(progress.begin(step: 2, stage: 1))
        progress.finish(first)
        XCTAssertFalse(progress.shouldAnnounce(step: 2, stage: 1))
        _ = progress.begin(step: 2, stage: 2)
        progress.cancel()
        XCTAssertTrue(progress.shouldAnnounce(step: 2, stage: 2))
        XCTAssertFalse(progress.shouldAnnounce(step: 2, stage: 1))
    }

    func testNewRouteAndManeuverDiscardOldPromptOwnership() throws {
        var progress = NavigationVoiceProgress()
        let first = try XCTUnwrap(progress.begin(step: 1, stage: 3))
        let next = try XCTUnwrap(progress.begin(step: 2, stage: 0))
        progress.finish(first)
        progress.cancel()
        XCTAssertTrue(progress.shouldAnnounce(step: 2, stage: 0))
        progress.reset()
        progress.finish(next)
        XCTAssertTrue(progress.shouldAnnounce(step: 1, stage: 0))
        XCTAssertNil(progress.begin(step: -1, stage: 0))
        XCTAssertNil(progress.begin(step: 1, stage: 4))
    }
}
