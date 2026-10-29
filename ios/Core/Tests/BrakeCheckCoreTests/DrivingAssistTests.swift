import XCTest
@testable import BrakeCheckCore

final class DrivingAssistTests: XCTestCase {
    func testAllPreferencesDefaultOffAndSubscriptionCannotEnableThemByItself() {
        let name = "DrivingAssistTests-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        XCTAssertEqual(DrivingAssistFeature.enabled(in: defaults, premium: true), [])
        for feature in DrivingAssistFeature.allCases { defaults.set(true, forKey: feature.preferenceKey) }
        XCTAssertEqual(DrivingAssistFeature.enabled(in: defaults, premium: false), [])
        XCTAssertEqual(DrivingAssistFeature.enabled(in: defaults, premium: true), Set(DrivingAssistFeature.offered))
        XCTAssertFalse(DrivingAssistFeature.enabled(in: defaults, premium: true).contains(.laneDeparture))
        XCTAssertFalse(DrivingAssistFeature.enabled(in: defaults, premium: true).contains(.cutIn))
    }

    func testTrafficStartRequiresStoppedLeadThenSustainedOpeningOnSameID() {
        var engine = DrivingAssistEngine()
        let id = UUID()
        for step in 0...12 {
            let time = Double(step) * 0.2
            XCTAssertNil(engine.updateLead(id: id, distance: 10, rangeRate: 0, confidence: 0.95,
                rearConfirmed: true, stationary: true, enabled: true, capturedAt: time, now: time))
        }
        var alerts: [DrivingAssistAlert] = []
        for step in 13...20 {
            let time = Double(step) * 0.2
            if let alert = engine.updateLead(id: id, distance: 11 + (time - 2.6), rangeRate: 1,
                confidence: 0.95, rearConfirmed: true, stationary: true, enabled: true,
                capturedAt: time, now: time) { alerts.append(alert) }
        }
        XCTAssertEqual(alerts.map(\.feature), [.trafficStart])
    }

    func testMovingEgoOrChangedLeadCannotTriggerTrafficStart() {
        var engine = DrivingAssistEngine()
        for step in 0...25 {
            let time = Double(step) * 0.2
            XCTAssertNil(engine.updateLead(id: UUID(), distance: 10 + time, rangeRate: 1,
                confidence: 0.95, rearConfirmed: true, stationary: false, enabled: true,
                capturedAt: time, now: time))
        }
    }

    private func lane(at time: Double, center: Double = 0.5, source: LaneSource = .neural) -> LanePath {
        LanePath(bands: (1...6).map { index in
            LaneBand(y: Double(index) * 0.1, left: center - 0.2, right: center + 0.2)
        }, confidence: 0.95, capturedAt: time, source: source)
    }

    func testLaneDepartureArmsCenteredThenRequiresGradualSustainedDrift() {
        var engine = DrivingAssistEngine()
        for step in 0...10 {
            let time = Double(step) * 0.2
            XCTAssertNil(engine.updateLane(lane(at: time), cameraCenter: 0.5, speed: 15,
                aligned: true, enabled: true, now: time))
        }
        var alerts: [DrivingAssistAlert] = []
        for step in 11...24 {
            let time = Double(step) * 0.2, center = 0.5 - min(0.14, Double(step - 10) * 0.02)
            if let alert = engine.updateLane(lane(at: time, center: center), cameraCenter: 0.5, speed: 15,
                aligned: true, enabled: true, now: time) { alerts.append(alert) }
        }
        XCTAssertEqual(alerts.map(\.feature), [.laneDeparture])
    }

    func testPaintFallbackUnalignedAndRepeatedCapturesCannotArmLaneDeparture() {
        for mode in 0...3 {
            var engine = DrivingAssistEngine()
            for step in 0...30 {
                let time = Double(step) * 0.2
                let captured = mode == 0 ? 0 : time
                let sample = lane(at: captured, center: step > 15 ? 0.36 : 0.5,
                                  source: mode == 1 ? .markings : .neural)
                XCTAssertNil(engine.updateLane(sample, cameraCenter: 0.5, speed: 15,
                    aligned: mode != 2, enabled: mode != 3, now: time))
            }
        }
    }

    private func vehicle(at time: Double, id: UUID, offset: Double) -> LeadVehicle {
        LeadVehicle(box: CGRect(x: 0.5 + offset * 0.4 - 0.03, y: 0.2, width: 0.06, height: 0.1),
            label: "car", confidence: 0.95,
            viewpoint: VehicleViewpoint(scores: [0, 0, 0, 0.9, 0, 0, 0, 0.1], capturedAt: time), trackID: id)
    }

    func testCutInNeedsConsistentInwardMotionFromSameRearFacingTrack() {
        var engine = DrivingAssistEngine()
        let id = UUID()
        var alerts: [DrivingAssistAlert] = []
        for step in 0...8 {
            let time = Double(step) * 0.2
            let observations = [vehicle(at: time, id: id, offset: 0.8 - 0.06 * Double(step))]
            if let alert = engine.updateVehicles(observations, lane: lane(at: time), speed: 12,
                enabled: true, capturedAt: time, now: time) { alerts.append(alert) }
        }
        XCTAssertEqual(alerts.map(\.feature), [.cutIn])
    }

    func testAdjacentParallelVehiclesAndTrackSwitchesDoNotPredictCutIn() {
        for changingID in [false, true] {
            var engine = DrivingAssistEngine()
            let id = UUID()
            for step in 0...10 {
                let time = Double(step) * 0.2
                let car = vehicle(at: time, id: changingID ? UUID() : id,
                                  offset: changingID ? 0.8 - 0.04 * Double(step) : 0.8)
                XCTAssertNil(engine.updateVehicles([car], lane: lane(at: time), speed: 12,
                    enabled: true, capturedAt: time, now: time))
            }
        }
    }

    private func signal(_ color: TrafficSignalColor, at time: Double,
                        x: Double = 0.45) -> TrafficSignalObservation {
        TrafficSignalObservation(box: CGRect(x: x, y: 0.65, width: 0.05, height: 0.15),
                                 color: color, capturedAt: time)
    }

    func testGreenSignalRequiresSustainedRedThenGreenOnSameHousing() {
        var engine = DrivingAssistEngine()
        var alerts: [DrivingAssistAlert] = []
        for step in 0...18 {
            let time = Double(step) * 0.2
            if let alert = engine.updateSignal(signal(step < 10 ? .red : .green, at: time),
                stationary: true, enabled: true, now: time) { alerts.append(alert) }
        }
        XCTAssertEqual(alerts.map(\.feature), [.greenLight])
    }

    func testGreenWithoutRedDifferentHousingMovementAndStaleFramesNeverNotify() {
        for mode in 0...4 {
            var engine = DrivingAssistEngine()
            for step in 0...20 {
                let time = Double(step) * 0.2
                let color: TrafficSignalColor = mode == 0 || step > 10 ? .green : .red
                let sample = signal(color, at: time, x: mode == 1 && step > 10 ? 0.7 : 0.45)
                XCTAssertNil(engine.updateSignal(sample, stationary: mode != 2, enabled: mode != 3,
                    now: mode == 4 ? time + 1 : time))
            }
        }
    }

    func testSignalLossClearsPreviouslyArmedRedAndDisabledStateResets() {
        var engine = DrivingAssistEngine()
        for step in 0...10 {
            let time = Double(step) * 0.2
            _ = engine.updateSignal(signal(.red, at: time), stationary: true, enabled: true, now: time)
        }
        XCTAssertNil(engine.updateSignal(nil, stationary: true, enabled: true, now: 2.2))
        for step in 12...22 {
            let time = Double(step) * 0.2
            XCTAssertNil(engine.updateSignal(signal(.green, at: time), stationary: true, enabled: true, now: time))
        }
    }

    func testSignalColorRejectsWhiteYellowAndMixedLamps() {
        func pixels(_ rgb: [UInt8]) -> [UInt8] {
            Array(repeating: rgb + [255], count: 16 * 32).flatMap { $0 }
        }
        XCTAssertEqual(TrafficSignalPixels.color(rgba: pixels([220, 25, 20]), width: 16, height: 32), .red)
        XCTAssertEqual(TrafficSignalPixels.color(rgba: pixels([20, 220, 60]), width: 16, height: 32), .green)
        for rgb: [UInt8] in [[240, 240, 240], [230, 190, 20], [10, 10, 10]] {
            XCTAssertEqual(TrafficSignalPixels.color(rgba: pixels(rgb), width: 16, height: 32), .unknown)
        }
        let mixed = Array(pixels([220, 25, 20]).prefix(16 * 16 * 4)) + Array(pixels([20, 220, 60]).prefix(16 * 16 * 4))
        XCTAssertEqual(TrafficSignalPixels.color(rgba: mixed, width: 16, height: 32), .unknown)
        XCTAssertEqual(TrafficSignalPixels.color(rgba: [], width: 16, height: 32), .unknown)
    }
}
