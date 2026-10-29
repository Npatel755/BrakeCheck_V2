import XCTest
@testable import BrakeCheckCore

final class DrivingAssistReadinessTests: XCTestCase {
    private func ready() -> DrivingAssistReadiness {
        var state = DrivingAssistReadiness()
        state.premium = true; state.enabled = true; state.liveTrip = true
        state.cameraReady = true; state.detectorReady = true; state.aligned = true
        state.freshStationaryGPS = true; state.freshMovingSpeed = 15
        state.freshLeadRange = true; state.freshRearVehicle = true; state.freshSignal = true
        state.now = 10
        state.lane = LanePath(bands: (1...6).map {
            LaneBand(y: Double($0) * 0.1, left: 0.3, right: 0.7)
        }, confidence: 0.95, capturedAt: 10, source: .neural)
        return state
    }

    func testStatusesExposeAccessAndLiveCaptureBlockersForEveryFeature() {
        let blockers: [(WritableKeyPath<DrivingAssistReadiness, Bool>, Bool, String)] = [
            (\.premium, false, "Premium access is off"), (\.enabled, false, "Off"),
            (\.liveTrip, false, "Start a live drive"), (\.replayOrDemo, true, "Requires a live drive"),
            (\.foreground, false, "Keep BrakeSense visible"), (\.cameraReady, false, "Waiting for the camera"),
            (\.detectorReady, false, "Waiting for vehicle detection"), (\.priorityAlert, true, "takes priority")
        ]
        for feature in DrivingAssistFeature.allCases {
            for (key, value, text) in blockers {
                var state = ready(); state[keyPath: key] = value
                XCTAssertTrue(state.message(for: feature).contains(text), "\(feature): \(text)")
            }
        }
    }

    func testStationaryRemindersExplainMissingGPSLeadAndSignalEvidence() {
        var state = ready(); state.freshStationaryGPS = false
        for feature in [DrivingAssistFeature.trafficStart, .greenLight] {
            XCTAssertTrue(state.message(for: feature).contains("GPS"))
        }
        state.freshStationaryGPS = true; state.freshLeadRange = false; state.freshSignal = false
        XCTAssertTrue(state.message(for: .trafficStart).contains("fresh gap"))
        XCTAssertTrue(state.message(for: .greenLight).contains("vertical traffic signal"))
    }

    func testLaneRemindersExplainAlignmentSpeedStaleOrDisplayOnlyLanesAndTurns() {
        var state = ready(); state.aligned = false
        XCTAssertTrue(state.message(for: .laneDeparture).contains("Camera alignment"))
        state.aligned = true; state.freshMovingSpeed = nil
        XCTAssertTrue(state.message(for: .laneDeparture).contains("reliable GPS"))
        state.freshMovingSpeed = 15; state.now = 11
        XCTAssertTrue(state.message(for: .laneDeparture).contains("fresh, reliable lane pair"))
        state = ready()
        state.lane = LanePath(bands: state.lane!.bands, confidence: 0.95, capturedAt: 10, source: .markings)
        XCTAssertTrue(state.message(for: .laneDeparture).contains("fresh, reliable lane pair"))
        state = ready(); state.maneuver = true
        XCTAssertTrue(state.message(for: .laneDeparture).contains("navigation turn"))
        state = ready(); state.freshRearVehicle = false
        XCTAssertTrue(state.message(for: .cutIn).contains("rear-facing adjacent"))
    }

    func testBannerExpiresAndCannotAppearWithoutLivePremiumOrOverPriorityUI() {
        func visible(premium: Bool = true, live: Bool = true, foreground: Bool = true,
                     replay: Bool = false, critical: Bool = false, checkin: Bool = false,
                     selecting: Bool = false, captured: Double = 10, now: Double = 11) -> Bool {
            DrivingAssistPresentation.showsAlert(premium: premium, liveTrip: live, foreground: foreground,
                replayOrDemo: replay, critical: critical, checkin: checkin, selecting: selecting,
                capturedAt: captured, now: now)
        }
        XCTAssertTrue(visible()); XCTAssertTrue(visible(now: 13))
        XCTAssertFalse(visible(now: 13.01)); XCTAssertFalse(visible(now: 9.9))
        XCTAssertFalse(visible(now: .nan)); XCTAssertFalse(visible(captured: .infinity))
        XCTAssertFalse(visible(premium: false)); XCTAssertFalse(visible(live: false))
        XCTAssertFalse(visible(foreground: false)); XCTAssertFalse(visible(replay: true))
        XCTAssertFalse(visible(critical: true)); XCTAssertFalse(visible(checkin: true))
        XCTAssertFalse(visible(selecting: true))
    }
}
