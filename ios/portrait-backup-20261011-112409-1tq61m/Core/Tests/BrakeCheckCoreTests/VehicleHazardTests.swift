import Foundation
import XCTest
@testable import BrakeCheckCore

final class VehicleHazardTests: XCTestCase {
    private let fov = 62.0
    private func car(_ range: Double = 20, center: Double = 0.5, id: UUID,
                     time: Double, view: VehicleViewpoint? = nil,
                     travel: VehicleTravelEvidence? = nil) -> LeadVehicle {
        let width = 1.8 / (2 * tan(fov * .pi / 360) * range)
        return LeadVehicle(box: CGRect(x: center - width / 2, y: 0.3,
            width: width, height: width * 16 / 9 / 1.5), label: "car", confidence: 0.94,
            viewpoint: view, trackID: id, travel: travel)
    }
    private func road(_ time: Double) -> RoadRegion {
        RoadRegion(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.1, right: 0.9) },
                   confidence: 0.9, capturedAt: time)
    }
    private func lane(_ time: Double, left: Double = 0.35, right: Double = 0.65) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: left, right: right) },
                 confidence: 0.9, capturedAt: time)
    }
    private func front(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.90, 0.03, 0.01, 0.01, 0.01, 0.01, 0.01, 0.02], capturedAt: time)!
    }
    private func approach(_ time: Double, closing: Double = 6, stable: Bool = true,
                          direction: VehicleTravelDirection = .uncertain) -> VehicleTravelEvidence {
        VehicleTravelEvidence(direction: direction, capturedAt: time, closingSpeed: closing,
                              isReliableApproach: true, hasStableBearing: stable)
    }

    func testApproachingStoppedCarDoesNotNeedForwardTargetMotion() throws {
        var guarder = VehicleMotionGuard()
        let id = UUID()
        var measured: LeadVehicle?
        for frame in 0...4 {
            let t = Double(frame) * 0.2, time = 100 + t
            let gps = GPSSpeedSample(metersPerSecond: 6, accuracy: 0.5, acceleration: nil,
                                     measurementUptime: time, simulated: false)
            measured = guarder.update([car(20 - 6 * t, id: id, time: time)],
                                     at: time, horizontalFOV: fov, ego: gps).first
        }
        let motion = try XCTUnwrap(measured?.travel)
        XCTAssertFalse(motion.supportsForwardTravel(at: 100.8))
        XCTAssertTrue(motion.supportsCollisionApproach(at: 100.8))
        XCTAssertTrue(motion.hasStableBearing)
        XCTAssertEqual(motion.closingSpeed, 6, accuracy: 0.0001)
    }

    func testCameraApproachSurvivesMissingGPSWithoutForwardDirectionClaim() throws {
        var guarder = VehicleMotionGuard()
        let id = UUID()
        var measured: LeadVehicle?
        for frame in 0...4 {
            let t = Double(frame) * 0.2, time = 100 + t
            measured = guarder.update([car(20 - 6 * t, id: id, time: time)],
                                     at: time, horizontalFOV: fov, ego: nil).first
        }
        let motion = try XCTUnwrap(measured?.travel)
        XCTAssertEqual(motion.direction, .uncertain)
        XCTAssertTrue(motion.supportsCollisionApproach(at: 100.8))
        XCTAssertFalse(motion.supportsForwardTravel(at: 100.8))
    }

    func testStoppedCameraAndStoppedCarDoNotInventAnApproach() throws {
        var guarder = VehicleMotionGuard()
        let id = UUID()
        var measured: LeadVehicle?
        for frame in 0...4 {
            let time = 100 + Double(frame) * 0.2
            measured = guarder.update([car(id: id, time: time)], at: time, horizontalFOV: fov, ego: nil).first
        }
        XCTAssertFalse(try XCTUnwrap(measured?.travel).supportsCollisionApproach(at: 100.8))
    }

    func testSidewaysBearingAndNonProportionalBoxGrowthAreRejectedForRoadHazards() throws {
        for yaw in [false, true] {
            var guarder = VehicleMotionGuard()
            let id = UUID()
            var measured: LeadVehicle?
            for frame in 0...4 {
                let t = Double(frame) * 0.2, time = 100 + t
                var vehicle = car(20 - 6 * t, center: yaw ? 0.5 : 0.5 + 0.15 * t,
                                  id: id, time: time)
                if yaw {
                    vehicle = LeadVehicle(box: CGRect(x: vehicle.box.minX, y: 0.3,
                        width: vehicle.box.width, height: 0.08), label: "car", confidence: 0.94, trackID: id)
                }
                measured = guarder.update([vehicle], at: time, horizontalFOV: fov, ego: nil).first
            }
            let motion = try XCTUnwrap(measured?.travel)
            if yaw { XCTAssertFalse(motion.isReliableApproach) }
            else { XCTAssertFalse(motion.hasStableBearing) }
        }
    }

    func testFrontFacingStationaryHazardWorksOnRoadWithoutPaintedLane() throws {
        var selector = VehicleHazardSelector()
        let id = UUID()
        var selection: LeadSelection?
        for time in [100.0, 100.1, 100.2] {
            selection = selector.update([car(id: id, time: time, view: front(time), travel: approach(time))],
                                        at: time, lane: nil, road: road(time), imageAspect: 16.0 / 9)
        }
        let hazard = try XCTUnwrap(selection)
        XCTAssertEqual(hazard.id, id)
        XCTAssertTrue(hazard.isCollisionHazard)
        XCTAssertTrue(hazard.isPathSupported)
        XCTAssertTrue(hazard.vehicle.viewpoint?.isFrontFacing(at: 100.2) == true)
    }

    func testRapidApproachCanBeAHazardButCannotBecomeALead() throws {
        var selector = VehicleHazardSelector()
        let id = UUID()
        var selection: LeadSelection?
        for time in [100.0, 100.1, 100.2] {
            selection = selector.update([car(id: id, time: time, view: front(time),
                travel: approach(time, closing: 20, direction: .rapidApproach))],
                at: time, lane: lane(time), road: nil, imageAspect: 16.0 / 9)
        }
        let hazard = try XCTUnwrap(selection)
        XCTAssertTrue(hazard.vehicle.travel?.vetoesLead(at: 100.2) == true)
        XCTAssertTrue(hazard.isCollisionHazard)
    }

    func testParkedVehicleBesideProjectedPathDoesNotBecomeHazard() {
        var selector = VehicleHazardSelector()
        let id = UUID()
        for time in [100.0, 100.1, 100.2, 100.3] {
            XCTAssertNil(selector.update([car(12, center: 0.78, id: id, time: time, travel: approach(time))],
                at: time, lane: nil, road: road(time), imageAspect: 16.0 / 9))
        }
    }

    func testParkedVehicleProtrudingIntoProjectedFootprintIsAHazard() throws {
        var selector = VehicleHazardSelector()
        let id = UUID()
        let center = 0.5 + 1.2 / (2 * tan(fov * .pi / 360) * 15)
        var selection: LeadSelection?
        for time in [100.0, 100.1, 100.2] {
            selection = selector.update([car(15, center: center, id: id, time: time, travel: approach(time))],
                at: time, lane: nil, road: road(time), imageAspect: 16.0 / 9)
        }
        XCTAssertTrue(try XCTUnwrap(selection).isCollisionHazard)
    }

    func testUnstableBearingCannotAuthorizeProjectedHazardWithOrWithoutRoad() {
        for mode in 0...2 {
            var selector = VehicleHazardSelector()
            let id = UUID()
            for time in [100.0, 100.1, 100.2, 100.3] {
                XCTAssertNil(selector.update([car(id: id, time: time,
                    travel: approach(time, stable: false))], at: time, lane: nil,
                    road: mode == 0 ? nil : road(mode == 1 ? time - 1 : time), imageAspect: 16.0 / 9))
            }
        }
    }

    func testStoppedVehicleApproachWarnsWithoutLaneRoadGPSOrRearClassifier() throws {
        var motion = VehicleMotionGuard(), selector = VehicleHazardSelector()
        let id = UUID()
        var hazard: LeadSelection?
        for frame in 0...7 {
            let elapsed = Double(frame) * 0.1, time = 100 + elapsed
            let vehicles = motion.update([car(10 - 6 * elapsed, id: id, time: time)],
                at: time, horizontalFOV: fov, ego: nil)
            hazard = selector.update(vehicles, at: time, lane: nil, road: nil, imageAspect: 16.0 / 9)
        }
        let selected = try XCTUnwrap(hazard)
        XCTAssertEqual(selected.id, id)
        XCTAssertTrue(selected.isCameraProjectedPath)
        XCTAssertTrue(selected.isCollisionHazard)
        let risk = CollisionApproachRules.evaluate(distance: 5.8,
            closingSpeed: selected.vehicle.travel?.closingSpeed, confidence: selected.vehicle.confidence,
            egoSpeed: nil, canEstimateLeadSpeed: false, isCameraProjectedPath: selected.isCameraProjectedPath)
        XCTAssertEqual(risk.level, .critical)
        XCTAssertEqual(try XCTUnwrap(risk.timeToCollision), 5.8 / 6, accuracy: 0.0001)
        XCTAssertNil(risk.leadAcceleration)
        XCTAssertTrue(risk.reason.contains("camera-projected"))
    }

    func testBothStoppedWithNoPathDoNotProduceAHazard() {
        var motion = VehicleMotionGuard(), selector = VehicleHazardSelector()
        let id = UUID()
        for frame in 0...12 {
            let time = 100 + Double(frame) * 0.1
            let vehicles = motion.update([car(10, id: id, time: time)],
                at: time, horizontalFOV: fov, ego: nil)
            XCTAssertNil(selector.update(vehicles, at: time, lane: nil, road: nil, imageAspect: 16.0 / 9))
        }
    }

    func testMissingAndStaleRoadCanUseExplicitCameraProjection() throws {
        for stale in [false, true] {
            var selector = VehicleHazardSelector()
            let id = UUID()
            var hazard: LeadSelection?
            for time in [100.0, 100.1, 100.2] {
                hazard = selector.update([car(id: id, time: time, travel: approach(time))],
                    at: time, lane: nil, road: stale ? road(time - 1) : nil, imageAspect: 16.0 / 9)
            }
            XCTAssertTrue(try XCTUnwrap(hazard).isCameraProjectedPath)
        }
    }

    func testMeasuredLaneAndRoadExclusionsOverrideCameraProjection() {
        for useLane in [false, true] {
            var selector = VehicleHazardSelector()
            let id = UUID()
            for time in [100.0, 100.1, 100.2, 100.3] {
                let offCenter = RoadRegion(bands: (0..<13).map {
                    LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.65, right: 0.95)
                }, confidence: 0.9, capturedAt: time)
                XCTAssertNil(selector.update([car(id: id, time: time, travel: approach(time))],
                    at: time, lane: useLane ? lane(time, left: 0.65, right: 0.95) : nil,
                    road: useLane ? nil : offCenter, imageAspect: 16.0 / 9))
            }
        }
    }

    func testSideAndBarelyOverlappingVehiclesCannotUseMissingRoadFallback() {
        for center in [0.57, 0.78] {
            var selector = VehicleHazardSelector()
            let id = UUID()
            for time in [100.0, 100.1, 100.2, 100.3] {
                XCTAssertNil(selector.update([car(20, center: center, id: id, time: time,
                    travel: approach(time))], at: time, lane: nil, road: nil, imageAspect: 16.0 / 9))
            }
        }
    }

    func testProjectedHazardNeedsFreshConfidentClosingEvidence() {
        for mode in 0...3 {
            var selector = VehicleHazardSelector()
            let id = UUID()
            for time in [100.0, 100.1, 100.2, 100.3] {
                let vehicle = car(id: id, time: time,
                    travel: approach(mode == 0 ? time - 1 : time, closing: mode == 1 ? 0 : 6,
                                     stable: mode != 2))
                let candidate = LeadVehicle(box: vehicle.box, label: "car",
                    confidence: mode == 3 ? 0.7 : 0.94, trackID: id, travel: vehicle.travel)
                XCTAssertNil(selector.update([candidate], at: time, lane: nil, road: nil, imageAspect: 16.0 / 9))
            }
        }
    }

    func testHazardReconfirmsAfterCaptureGapOrImageRotation() {
        for rotate in [false, true] {
            var selector = VehicleHazardSelector()
            let id = UUID()
            for time in [100.0, 100.1, 100.2] {
                _ = selector.update([car(id: id, time: time, travel: approach(time))],
                    at: time, lane: nil, road: nil, imageAspect: 16.0 / 9)
            }
            let time = rotate ? 100.3 : 101
            let old = car(id: id, time: time, travel: approach(time))
            let candidate = LeadVehicle(box: rotate ? CGRect(x: old.box.minX, y: 0.3,
                width: old.box.width, height: old.box.height * 16 / 9) : old.box,
                label: "car", confidence: 0.94, trackID: id, travel: old.travel)
            XCTAssertNil(selector.update([candidate], at: time, lane: nil, road: nil,
                imageAspect: rotate ? 9.0 / 16 : 16.0 / 9))
        }
    }

    func testMeasuredCurvedLaneCanAuthorizeOffCenterHazard() throws {
        var selector = VehicleHazardSelector()
        let id = UUID()
        var selection: LeadSelection?
        for time in [100.0, 100.1, 100.2] {
            selection = selector.update([car(12, center: 0.7, id: id, time: time,
                travel: approach(time, stable: false))], at: time,
                lane: lane(time, left: 0.6, right: 0.8), road: nil, imageAspect: 16.0 / 9)
        }
        XCTAssertTrue(try XCTUnwrap(selection).isCollisionHazard)
    }

    func testShortestClosingTimeWinsOverClosestDistance() throws {
        var selector = VehicleHazardSelector()
        let near = UUID(), urgent = UUID()
        var selection: LeadSelection?
        for time in [100.0, 100.1, 100.2] {
            selection = selector.update([
                car(10, center: 0.42, id: near, time: time, travel: approach(time, closing: 1)),
                car(20, center: 0.58, id: urgent, time: time, travel: approach(time, closing: 20))],
                at: time, lane: lane(time), road: nil, imageAspect: 16.0 / 9)
        }
        XCTAssertEqual(try XCTUnwrap(selection).id, urgent)
        XCTAssertNil(selector.update([], at: 100.3, lane: lane(100.3), road: nil, imageAspect: 16.0 / 9))
    }

    func testCollisionTimeDoesNotNeedBrakingAbsoluteSpeedOrExactWidthScale() throws {
        for scale in [0.7, 1.0, 1.45] {
            let risk = CollisionApproachRules.evaluate(distance: 10 * scale, closingSpeed: 10 * scale,
                confidence: 0.94, egoSpeed: nil, canEstimateLeadSpeed: false)
            XCTAssertEqual(try XCTUnwrap(risk.timeToCollision), 1, accuracy: 0.0001)
            XCTAssertEqual(risk.level, .critical)
            XCTAssertNil(risk.leadSpeed); XCTAssertNil(risk.leadAcceleration)
        }
        let stationary = CollisionApproachRules.evaluate(distance: 10, closingSpeed: 10,
            confidence: 0.94, egoSpeed: 10, canEstimateLeadSpeed: true)
        XCTAssertEqual(stationary.leadSpeed, 0)
        XCTAssertEqual(stationary.level, .critical)
        let uncertainSpeed = CollisionApproachRules.evaluate(distance: 14.5, closingSpeed: 14.5,
            confidence: 0.94, egoSpeed: 10, canEstimateLeadSpeed: true)
        XCTAssertNil(uncertainSpeed.leadSpeed)
        XCTAssertEqual(uncertainSpeed.level, .critical)
    }

    func testHazardRiskPreservesExistingLeadBrakingAndHeadway() {
        let lead = RiskSnapshot(level: .high, score: 65, distance: 10, closingSpeed: 2,
            leadSpeed: 8, leadAcceleration: -4, headway: 1.2)
        let approach = CollisionApproachRules.evaluate(distance: 10, closingSpeed: 10,
            confidence: 0.94, egoSpeed: nil, canEstimateLeadSpeed: false)
        let result = CollisionApproachRules.combine(lead: lead, approach: approach)
        XCTAssertEqual(result.level, .critical)
        XCTAssertEqual(result.leadAcceleration, -4)
        XCTAssertEqual(result.headway, 1.2)
        XCTAssertEqual(result.leadSpeed, 8)
    }
}
