import Foundation
import XCTest
@testable import BrakeCheckCore

/// Synthetic detector boxes and classifier scores inspired by the traffic,
/// misses and delayed acquisition visible in Video1.mp4. These tests exercise
/// the real core pipeline; they do not replay pixels or run CoreML inference.
final class DrivingPipelineContinuityTests: XCTestCase {
    private struct Observation {
        let role: String
        let vehicle: LeadVehicle
    }

    private struct Frame {
        let vehicles: [String: LeadVehicle]
        let selection: LeadSelection?
        let camera: CameraRangeSnapshot
        let risk: RiskSnapshot
        let danger: DangerMeterSnapshot
        let acceptedClassifierResults: Int
    }

    private struct Pipeline {
        var headings = VehicleHeadingTracks()
        var ranges = VehicleRangeHistory()
        var motion = VehicleMotionGuard()
        var selector = LeadSelector(requiresRearView: true)
        var riskEstimator = LeadRiskContinuity()
        var dangerContinuity = LeadDangerContinuity()
        var selectedID: UUID?
        var pendingClassifications: [(UUID, VehicleViewpoint)] = []

        mutating func step(_ observations: [Observation], at time: Double,
                           visualLeadBox: CGRect? = nil, classify roles: Set<String> = []) -> Frame {
            let linked = headings.update(observations.map(\.vehicle), at: time,
                preferredID: selectedID, trackedBox: visualLeadBox)
            var accepted = 0
            // A result describes the previous capture, never a new orientation
            // sample for every repeated YOLO frame. Delivery occurs only after
            // association has checked the current capture's vehicle identity.
            for (id, prediction) in pendingClassifications {
                if headings.record(prediction, for: id, at: time) { accepted += 1 }
            }
            pendingClassifications = []
            let classified = linked.map { vehicle in
                LeadVehicle(box: vehicle.box, label: vehicle.label, confidence: vehicle.confidence,
                    viewpoint: vehicle.trackID.flatMap { headings.viewpoint(for: $0, at: time) },
                    trackID: vehicle.trackID)
            }
            let vehicles = Dictionary(uniqueKeysWithValues: zip(observations, classified).map { pair in
                (pair.0.role, pair.1)
            })
            let gps = GPSSpeedSample(metersPerSecond: 20, accuracy: 0.3, acceleration: 0,
                measurementUptime: time, simulated: false)
            let guarded = motion.update(classified, at: time, horizontalFOV: 60, ego: gps)
            let captures = ranges.update(guarded, at: time, horizontalFOV: 60, detectorGeneration: 0)
            let selection = selector.update(guarded, at: time, lane: Fixtures.lane(time),
                trackedBox: visualLeadBox, imageAspect: 16.0 / 9)
            if selectedID != selection?.id { riskEstimator.reset() }
            selectedID = selection?.id

            var camera = CameraRangeSnapshot(reason: "Detection gap · camera numbers paused")
            var risk = RiskSnapshot(reason: "Waiting for a proven eligible lead")
            if let selection {
                if selection.isFreshDetection, selection.isPathSupported, selection.hasConfirmedRearIdentity,
                   let capture = captures[selection.id], capture.isCurrent(for: selection.id, at: time),
                   let distance = capture.snapshot.distance {
                    camera = capture.snapshot
                    let lead = riskEstimator.update(time: time, distance: distance,
                        egoSpeed: gps.metersPerSecond, gpsAge: 0, confidence: selection.vehicle.confidence,
                        camera: camera)
                    risk = CollisionApproachRules.forConfirmedLead(lead: lead, camera: camera,
                        confidence: selection.vehicle.confidence, fresh: true,
                        pathSupported: true, rearConfirmed: true)
                } else {
                    risk = riskEstimator.pause(at: time, reason: "Detection or eligibility gap")
                }
            }
            let danger = dangerContinuity.update(targetID: selectedID, capturedAt: time, now: time, risk: risk)
            for role in roles {
                if let id = vehicles[role]?.trackID {
                    pendingClassifications.append((id, Fixtures.rear(time)))
                }
            }
            return Frame(vehicles: vehicles, selection: selection, camera: camera, risk: risk,
                danger: danger, acceptedClassifierResults: accepted)
        }
    }

    private enum Fixtures {
        static func vehicle(_ role: String, distance: Double, center: Double,
                            label: String = "car", bottom: Double = 0.28) -> Observation {
            let metersWide = label == "car" ? 1.8 : 2.5
            let width = metersWide / (2 * tan(Double.pi / 6) * distance)
            let height = width * (16.0 / 9) / 1.5
            return Observation(role: role, vehicle: LeadVehicle(
                box: CGRect(x: center - width / 2, y: bottom, width: width, height: height),
                label: label, confidence: 0.93))
        }
        static func lane(_ time: Double) -> LanePath {
            LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.35, right: 0.65) },
                confidence: 0.92, capturedAt: time)
        }
        static func rear(_ time: Double) -> VehicleViewpoint {
            VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!
        }
        static func leadDistance(_ time: Double) -> Double {
            let brakingTime = max(0, time - 22)
            return 42 - 0.35 * time - 1.2 * brakingTime * brakingTime
        }
    }

    func testSynthetic26SecondDriveKeepsEligibleLeadAndFreshDangerThroughTrafficAndMisses() throws {
        var pipeline = Pipeline()
        // Five Hz, then two Hz for four seconds, then five Hz while the lead
        // slows. Every point is a fresh capture, separate from classifier timing.
        let ticks = Array(stride(from: 0, through: 178, by: 2))
            + Array(stride(from: 180, through: 220, by: 5))
            + Array(stride(from: 222, through: 260, by: 2))
        let missingLeadTicks: Set<Int> = [60, 100, 102, 140]
        var expectedID: UUID?
        var previousDanger: Double?
        var gaps = 0, retainedRearFrames = 0, slowFreshFrames = 0, scoredFreshFrames = 0
        var deliveredRearResults = 0
        var final = Frame(vehicles: [:], selection: nil, camera: CameraRangeSnapshot(),
            risk: RiskSnapshot(), danger: DangerMeterSnapshot(), acceptedClassifierResults: 0)
        for tick in ticks {
            let time = Double(tick) / 10
            let lead = Fixtures.vehicle("lead", distance: Fixtures.leadDistance(time), center: 0.5, label: "truck")
            // Both adjacent cars are closer than the lead and receive rear
            // proof, so lane eligibility rather than absent proof excludes them.
            let passing = Fixtures.vehicle("passing", distance: 14 + 0.3 * time,
                center: 0.18 + 0.012 * sin(time), bottom: 0.20)
            let adjacent = Fixtures.vehicle("adjacent", distance: 18 + 0.1 * time,
                center: 0.82, bottom: 0.22)
            let observations = missingLeadTicks.contains(tick) ? [passing, adjacent] : [lead, passing, adjacent]
            let classifyNow = tick <= 20 ? tick.isMultiple(of: 4) : tick.isMultiple(of: 20)
            final = pipeline.step(observations, at: time,
                visualLeadBox: expectedID == nil ? nil : lead.vehicle.box,
                classify: classifyNow ? Set(observations.map(\.role)) : [])
            deliveredRearResults += final.acceptedClassifierResults
            if expectedID == nil { expectedID = final.vehicles["lead"]?.trackID }
            if time >= 1.2 {
                let selection = try XCTUnwrap(final.selection, "Lead disappeared at \(time) s")
                XCTAssertEqual(selection.id, expectedID, "Wrong lead at \(time) s")
                XCTAssertNotEqual(selection.id, final.vehicles["passing"]?.trackID)
                XCTAssertNotEqual(selection.id, final.vehicles["adjacent"]?.trackID)
                XCTAssertTrue(selection.hasConfirmedRearIdentity)
                if selection.isRearConfirmationRetained { retainedRearFrames += 1 }
                if missingLeadTicks.contains(tick) {
                    gaps += 1
                    XCTAssertFalse(selection.isFreshDetection)
                    XCTAssertNil(final.camera.distance); XCTAssertNil(final.camera.closingSpeed)
                    XCTAssertNil(final.risk.distance); XCTAssertNil(final.risk.leadSpeed)
                    XCTAssertNil(final.risk.timeToCollision); XCTAssertNil(final.risk.score)
                    XCTAssertEqual(final.danger.score, previousDanger)
                    XCTAssertTrue(final.danger.isHeld)
                } else {
                    XCTAssertTrue(selection.isFreshDetection)
                    XCTAssertTrue(selection.isPathSupported)
                    XCTAssertTrue(TrackingFreshness.measurementIsFresh(capturedAt: time, now: time + 0.05))
                    XCTAssertFalse(TrackingFreshness.measurementIsFresh(capturedAt: time, now: time + 0.61))
                    if time < 22 || time >= 24 {
                        XCTAssertNotNil(final.risk.score, "No risk on measured lead at \(time) s")
                        XCTAssertEqual(try XCTUnwrap(final.camera.distance), Fixtures.leadDistance(time), accuracy: 0.0001)
                        scoredFreshFrames += 1
                    }
                    if (18...22).contains(time) {
                        XCTAssertNotNil(final.risk.leadSpeed, "Slow cadence restarted speed at \(time) s")
                        slowFreshFrames += 1
                    }
                }
            }
            previousDanger = final.danger.score
        }
        XCTAssertEqual(gaps, 4)
        XCTAssertGreaterThan(retainedRearFrames, 30)
        XCTAssertGreaterThan(scoredFreshFrames, 90)
        XCTAssertEqual(slowFreshFrames, 9)
        XCTAssertGreaterThan(deliveredRearResults, 20)
        XCTAssertEqual(final.risk.level, .critical)
        XCTAssertLessThan(try XCTUnwrap(final.risk.timeToCollision), 1.5)
        XCTAssertEqual(final.danger.level, .critical)
    }

    func testProvenCandidateHandoffUsesItsOwnMotionAndClearsPreviousDanger() throws {
        var pipeline = Pipeline()
        var originalID: UUID?, replacementID: UUID?, handoff: Frame?
        var sawOriginalCritical = false, sawCandidateWithOnlyOneRearCapture = false
        for tick in stride(from: 0, through: 64, by: 2) {
            let time = Double(tick) / 10
            let transition = min(1, max(0, (time - 4) / 2))
            let original = Fixtures.vehicle("original", distance: 26 - 3 * time,
                center: 0.5 - 0.30 * transition)
            let candidate = Fixtures.vehicle("candidate", distance: 50,
                center: 0.5, bottom: 0.44)
            // A farther in-lane car becomes visible as the old lead moves
            // gradually out of the lane. Its distinct scale and ground contact
            // let HeadingTracks associate real detections without teleporting
            // a small silhouette across the image between classifier captures.
            let observations = time < 4 ? [original] : [original, candidate]
            var classify: Set<String> = tick <= 20 && tick.isMultiple(of: 4) ? ["original"] : []
            if tick == 40 || tick == 46 { classify.insert("candidate") }
            let visual = pipeline.selectedID == replacementID && replacementID != nil
                ? candidate.vehicle.box : pipeline.selectedID != nil ? original.vehicle.box : nil
            let frame = pipeline.step(observations, at: time, visualLeadBox: visual, classify: classify)
            if originalID == nil { originalID = frame.vehicles["original"]?.trackID }
            if replacementID == nil { replacementID = frame.vehicles["candidate"]?.trackID }
            if frame.selection?.id == originalID && frame.risk.level == .critical { sawOriginalCritical = true }
            if let candidateView = frame.vehicles["candidate"]?.viewpoint, candidateView.rearEvidenceCount == 1 {
                sawCandidateWithOnlyOneRearCapture = true
                XCTAssertNotEqual(frame.selection?.id, replacementID, "One classifier capture acquired the new lead")
            }
            if let selected = frame.selection, selected.id == replacementID, handoff == nil {
                handoff = frame
                XCTAssertTrue(selected.hasConfirmedRearIdentity)
                XCTAssertTrue(selected.isFreshDetection)
                XCTAssertTrue(selected.isPathSupported)
            }
            if time < 4, time >= 1.2 { XCTAssertEqual(frame.selection?.id, originalID) }
        }
        XCTAssertTrue(sawOriginalCritical)
        XCTAssertTrue(sawCandidateWithOnlyOneRearCapture)
        XCTAssertNotEqual(originalID, replacementID)
        let firstNewLead = try XCTUnwrap(handoff)
        XCTAssertEqual(try XCTUnwrap(firstNewLead.camera.distance), 50, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(firstNewLead.camera.closingSpeed), 0, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(firstNewLead.risk.leadSpeed), 20, accuracy: 0.0001)
        XCTAssertNil(firstNewLead.risk.leadAcceleration)
        XCTAssertNil(firstNewLead.risk.timeToCollision)
        XCTAssertEqual(firstNewLead.risk.level, .low)
        XCTAssertEqual(firstNewLead.danger.level, .low)
        XCTAssertEqual(firstNewLead.danger.score, 0)
        XCTAssertFalse(firstNewLead.danger.isHeld)
    }
}
