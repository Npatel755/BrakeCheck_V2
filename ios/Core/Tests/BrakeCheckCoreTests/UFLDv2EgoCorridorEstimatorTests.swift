import XCTest
@testable import BrakeCheckCore

final class UFLDv2EgoCorridorEstimatorTests: XCTestCase {
    private func candidate(slot: Int, anchors: [Int] = Array(0..<71), positive: Int = 72,
                           probability: Double = 0.95, bend: Double = 0,
                           x override: ((Double, Int) -> Double)? = nil) -> UFLDv2LaneDecoder.Candidate {
        .init(laneSlot: slot, axis: .row, positiveAnchors: positive,
              meanExistenceProbability: probability, points: anchors.map { index in
            let y = 1 - UFLDv2LaneDecoder.rowAnchors[index]
            let half = 0.30 - 0.44 * y
            let center = 0.5 + bend * pow(y / 0.55, 2)
            return .init(x: override?(y, index) ?? (slot == 1 ? center - half : center + half),
                         y: y, strength: probability, anchorIndex: index)
        })
    }
    private func pair(bend: Double = 0) -> [UFLDv2LaneDecoder.Candidate] {
        [candidate(slot: 1, bend: bend), candidate(slot: 2, bend: bend)]
    }
    private func confirm(_ estimator: inout UFLDv2EgoCorridorEstimator,
                         candidates: [UFLDv2LaneDecoder.Candidate]? = nil,
                         excluding boxes: [CGRect] = [], at time: Double = 10) throws -> LanePath {
        let input = candidates ?? pair()
        XCTAssertNil(estimator.update(input, excluding: boxes, at: time))
        return try XCTUnwrap(estimator.update(input, excluding: boxes, at: time + 0.2))
    }
    private func rear(at time: Double) -> VehicleViewpoint {
        let latest = VehicleViewpoint(scores: [0,0,0,0,1,0,0,0], capturedAt: time)!
        return VehicleViewpoint(latest: latest, rearCount: 3, rearSpan: 0.3)
    }
    private func car(id: UUID, x: Double, at time: Double, forward: Bool = true) -> LeadVehicle {
        LeadVehicle(box: CGRect(x: x - 0.04, y: 0.35, width: 0.08, height: 0.07),
            label: "car", confidence: 0.9, viewpoint: rear(at: time), trackID: id,
            travel: forward ? VehicleTravelEvidence(direction: .forwardConsistent,
                capturedAt: time, closingSpeed: 0) : nil)
    }
    private func road(at time: Double) -> RoadRegion {
        RoadRegion(bands: (0..<20).map { index in
            LaneBand(y: 0.04 + Double(index) * 0.028, left: 0.05, right: 0.95)
        }, confidence: 0.95, capturedAt: time)
    }

    func testTwoDistinctCapturesConfirmTaggedPair() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let lane = try confirm(&estimator)
        XCTAssertEqual(lane.source, .neural)
        XCTAssertEqual(lane.capturedAt, 10.2)
        XCTAssertTrue(lane.isUsable(at: 10.2))
        XCTAssertEqual(lane.confidence, 0.95, accuracy: 1e-8)
        XCTAssertTrue(estimator.status.contains("confirmed"))
    }
    func testConfirmedPairSurvivesOneDroppedCaptureButNotTwoOrALongGap() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        _ = try confirm(&estimator)
        // One capture with a missing right head: nothing emitted, history kept.
        XCTAssertNil(estimator.update([candidate(slot: 1)], at: 10.5))
        XCTAssertTrue(estimator.status.contains("holding"))
        XCTAssertNotNil(estimator.update(pair(), at: 10.8))
        // Two consecutive misses require a fresh two-capture confirmation.
        XCTAssertNil(estimator.update([candidate(slot: 1)], at: 11.1))
        XCTAssertNil(estimator.update([], at: 11.4))
        XCTAssertNil(estimator.update(pair(), at: 11.7))
        XCTAssertNotNil(estimator.update(pair(), at: 12.0))
        // A miss after a long silence does not hold.
        XCTAssertNil(estimator.update([candidate(slot: 2)], at: 12.0 + UFLDv2EgoCorridorEstimator.missTolerance + 0.1))
        XCTAssertNil(estimator.update(pair(), at: 13.0))
        XCTAssertNotNil(estimator.update(pair(), at: 13.3))
    }
    func testSlotWithSeveralPiecesUsesItsBestSupportedPiece() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let fragment = candidate(slot: 2, anchors: Array(0..<6), positive: 6)
        XCTAssertNil(estimator.update([candidate(slot: 1), fragment, candidate(slot: 2)], at: 10))
        XCTAssertNotNil(estimator.update([candidate(slot: 1), candidate(slot: 2), fragment], at: 10.2))
    }
    func testSingleZigzagHeadCannotInventOppositeBoundary() {
        let zigzag = candidate(slot: 1, x: { y, index in
            let half = 0.30 - 0.44 * y
            return index.isMultiple(of: 2) ? 0.5 - half : 0.5 + half
        })
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update([zigzag], at: 10))
        XCTAssertNil(estimator.update([zigzag], at: 10.2))
    }
    func testZigzagIsNotValidEvenWhenOtherEgoHeadExists() {
        let zigzag = candidate(slot: 1, x: { _, index in index.isMultiple(of: 2) ? 0.25 : 0.65 })
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update([zigzag, candidate(slot: 2)], at: 10))
        XCTAssertNil(estimator.update([zigzag, candidate(slot: 2)], at: 10.2))
    }
    func testSwappedEgoHeadsAreRejectedRatherThanSortedByX() {
        let left = candidate(slot: 1, x: { y, _ in 0.5 + 0.30 - 0.44 * y })
        let right = candidate(slot: 2, x: { y, _ in 0.5 - 0.30 + 0.44 * y })
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update([left,right], at: 10))
        XCTAssertNil(estimator.update([left,right], at: 10.2))
    }
    func testCrossingEgoHeadsAreRejected() {
        let input = [candidate(slot: 1, x: { y,_ in 0.25 + 0.6 * y }),
                     candidate(slot: 2, x: { y,_ in 0.75 - 0.6 * y })]
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update(input, at: 10))
        XCTAssertNil(estimator.update(input, at: 10.2))
    }
    func testOuterSlotsNeverSubstituteForMissingEgoHead() {
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update([candidate(slot: 0),candidate(slot: 3)], at: 10))
        XCTAssertNil(estimator.update([candidate(slot: 1),candidate(slot: 3)], at: 10.2))
    }
    func testExtraOuterBoundariesDoNotMoveEgoPair() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let input = pair() + [candidate(slot: 0, x: { _,_ in 0.01 }),candidate(slot: 3, x: { _,_ in 0.98 })]
        let lane = try confirm(&estimator, candidates: input)
        let bounds = try XCTUnwrap(lane.bounds(at: 0.30))
        XCTAssertEqual(bounds.left, 0.332, accuracy: 1e-8)
        XCTAssertEqual(bounds.right, 0.668, accuracy: 1e-8)
    }
    func testEachHeadRequiresStockExistenceGate() {
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update([candidate(slot: 1, positive: 36),candidate(slot: 2)], at: 10))
        XCTAssertNil(estimator.update([candidate(slot: 1),candidate(slot: 2, positive: 36)], at: 10.2))
    }
    func testDuplicateAndWrongAxisHeadsCannotConfirm() {
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update(pair() + [candidate(slot: 1)], at: 10))
        let item = candidate(slot: 2)
        let wrong = UFLDv2LaneDecoder.Candidate(laneSlot: 2, axis: .column, positiveAnchors: 72,
            meanExistenceProbability: 0.95, points: item.points)
        XCTAssertNil(estimator.update([candidate(slot: 1),wrong], at: 10.2))
    }
    func testNonOverlappingSpansCannotFormPair() {
        var estimator = UFLDv2EgoCorridorEstimator()
        let input = [candidate(slot: 1, anchors: Array(0...30)),candidate(slot: 2, anchors: Array(40...70))]
        XCTAssertNil(estimator.update(input, at: 10))
        XCTAssertNil(estimator.update(input, at: 10.2))
    }
    func testTooShortSharedSpanCannotFormCorridor() {
        var estimator = UFLDv2EgoCorridorEstimator()
        let input = [candidate(slot: 1, anchors: Array(0...10)),candidate(slot: 2, anchors: Array(0...10))]
        XCTAssertNil(estimator.update(input, at: 10))
        XCTAssertNil(estimator.update(input, at: 10.2))
    }
    func testCurvedPairKeepsObservedEndpointsWithoutExtrapolation() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let input = [candidate(slot: 1, anchors: Array(10...60), bend: 0.18),
                     candidate(slot: 2, anchors: Array(15...65), bend: 0.18)]
        let lane = try confirm(&estimator, candidates: input)
        let bottom = 1 - UFLDv2LaneDecoder.rowAnchors[60]
        let top = 1 - UFLDv2LaneDecoder.rowAnchors[15]
        XCTAssertEqual(lane.bands.first!.y, bottom, accuracy: 1e-9)
        XCTAssertEqual(lane.bands.last!.y, top, accuracy: 1e-9)
        XCTAssertNil(lane.bounds(at: bottom - 0.001))
        XCTAssertNil(lane.bounds(at: top + 0.001))
    }
    func testMaskedEgoHeadCannotBeReplacedByOuterHead() {
        var estimator = UFLDv2EgoCorridorEstimator()
        let masks = [CGRect(x: 0,y: 0,width: 0.49,height: 0.65)]
        let input = pair() + [candidate(slot: 0),candidate(slot: 3)]
        XCTAssertNil(estimator.update(input, excluding: masks, at: 10))
        XCTAssertNil(estimator.update(input, excluding: masks, at: 10.2))
    }
    func testMaskSplitsSupportInsteadOfBridgingVehicle() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let mask = CGRect(x: 0.29,y: 0.28,width: 0.08,height: 0.05)
        let lane = try confirm(&estimator, excluding: [mask])
        XCTAssertTrue(lane.bands.last!.y < mask.minY || lane.bands.first!.y > mask.maxY)
    }
    func testUnsupportedAnchorGapIsNotBridged() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let anchors = Array(0...20) + Array(45...70)
        let lane = try confirm(&estimator, candidates: [candidate(slot: 1,anchors: anchors),candidate(slot: 2,anchors: anchors)])
        XCTAssertLessThan(lane.bands.last!.y - lane.bands.first!.y, 0.25)
    }
    func testHeadLossHoldsOnceThenRequiresTwoNewCaptures() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        _ = try confirm(&estimator)
        XCTAssertNil(estimator.update([candidate(slot: 1)], at: 10.3))
        XCTAssertNotNil(estimator.update(pair(), at: 10.4))
        XCTAssertNil(estimator.update([candidate(slot: 1)], at: 10.6))
        XCTAssertNil(estimator.update([candidate(slot: 1)], at: 10.8))
        XCTAssertNil(estimator.update(pair(), at: 10.9))
        XCTAssertNotNil(estimator.update(pair(), at: 11.0))
    }
    func testEmptyNegativeNeverEmitsOrRenewsTheHeldCapture() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let previous = try confirm(&estimator)
        XCTAssertNil(estimator.update([], at: 10.3))
        XCTAssertEqual(previous.capturedAt, 10.2)
        let next = try XCTUnwrap(estimator.update(pair(), at: 10.4))
        XCTAssertEqual(next.capturedAt, 10.4)
        XCTAssertNil(estimator.update([], at: 10.6))
        XCTAssertNil(estimator.update([], at: 10.8))
        XCTAssertNil(estimator.update(pair(), at: 10.9))
    }
    func testLargePairShiftMustBeConfirmedAgain() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        _ = try confirm(&estimator)
        let shifted = [candidate(slot: 1,x: { y,_ in 0.65 - 0.30 + 0.44 * y }),
                       candidate(slot: 2,x: { y,_ in 0.65 + 0.30 - 0.44 * y })]
        XCTAssertNil(estimator.update(shifted, at: 10.3))
        XCTAssertNotNil(estimator.update(shifted, at: 10.4))
    }
    func testDuplicateAndReversedTimesCannotConfirm() {
        var estimator = UFLDv2EgoCorridorEstimator()
        XCTAssertNil(estimator.update(pair(), at: 10))
        XCTAssertNil(estimator.update(pair(), at: 10))
        XCTAssertNil(estimator.update(pair(), at: 9.9))
        XCTAssertNil(estimator.update(pair(), at: 9.8))
    }
    func testNonFiniteTimeAndResetNeedNewConfirmation() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        _ = try confirm(&estimator)
        XCTAssertNil(estimator.update(pair(), at: .nan))
        XCTAssertNil(estimator.update(pair(), at: 11))
        estimator.reset()
        XCTAssertNil(estimator.update(pair(), at: 12))
        XCTAssertNotNil(estimator.update(pair(), at: 12.2))
    }
    func testCaptureTimestampExpiresAndLongGapReconfirms() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let lane = try confirm(&estimator)
        XCTAssertEqual(lane.capturedAt, 10.2)
        XCTAssertFalse(lane.isUsable(at: 10.701))
        XCTAssertNil(estimator.update(pair(), at: 12))
        XCTAssertNotNil(estimator.update(pair(), at: 12.2))
    }
    func testTaggedCurveSupportsRearLeadAndRejectsAdjacentVehicle() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        var selector = LeadSelector(requiresRearView: true)
        let leadID = UUID(), adjacentID = UUID(), bend = 0.18
        let center = 0.5 + bend * pow(0.35 / 0.55, 2)
        var selected: LeadSelection?
        for index in 0...5 {
            let time = 10 + Double(index) * 0.1
            let lane = estimator.update(pair(bend: bend), at: time)
            selected = selector.update([car(id: adjacentID,x: center + 0.25,at: time),car(id: leadID,x: center,at: time)],
                at: time, lane: lane, road: road(at: time))
        }
        let lead = try XCTUnwrap(selected)
        XCTAssertEqual(lead.id, leadID)
        XCTAssertTrue(lead.isPathSupported)
    }
    func testSingleHeadKeepsForwardRoadFallbackWithoutInventingLane() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        var selector = LeadSelector(requiresRearView: true)
        let leadID = UUID(), adjacentID = UUID()
        var selected: LeadSelection?
        for index in 0...4 {
            let time = 10 + Double(index) * 0.1
            let lane = estimator.update([candidate(slot: 1)], at: time)
            XCTAssertNil(lane)
            selected = selector.update([car(id: adjacentID,x: 0.75,at: time),car(id: leadID,x: 0.5,at: time)],
                at: time, lane: lane, road: road(at: time))
        }
        let lead = try XCTUnwrap(selected)
        XCTAssertEqual(lead.id, leadID)
        XCTAssertTrue(lead.isPathSupported)
    }
    func testRoadAndRearViewAloneDoNotAuthorizePath() throws {
        var selector = LeadSelector(requiresRearView: true)
        let leadID = UUID()
        var selected: LeadSelection?
        for index in 0...4 {
            let time = 10 + Double(index) * 0.1
            selected = selector.update([car(id: leadID,x: 0.5,at: time,forward: false)],
                at: time, road: road(at: time))
        }
        XCTAssertFalse(try XCTUnwrap(selected).isPathSupported)
    }
    func testRetainedRearIdentityCannotKeepRoadPathAuthorizationAfterMovingAdjacent() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        var selection: LeadSelection?
        // First acquire the same rear-confirmed, forward-moving vehicle in the
        // camera footprint, then observe it moving into an adjacent lane. A
        // broad pavement region continues to contain both vehicle positions.
        for index in 0...16 {
            let time = 10 + Double(index) * 0.1
            let x = index <= 3 ? 0.5 : min(0.75, 0.5 + Double(index - 3) * 0.02)
            selection = selector.update([car(id: id,x: x,at: time)], at: time, road: road(at: time))
            if index == 3 { XCTAssertTrue(try XCTUnwrap(selection).isPathSupported) }
        }
        let retained = try XCTUnwrap(selection)
        XCTAssertEqual(retained.id, id)
        XCTAssertTrue(retained.isFreshDetection)
        XCTAssertTrue(retained.hasConfirmedRearIdentity)
        XCTAssertFalse(LeadVehicleGeometry.supportsBlindLead(retained.vehicle))
        XCTAssertFalse(retained.isPathSupported,
            "Road membership and forward motion alone do not locate an already tracked vehicle in our lane")
    }

    func testUnmarkedRoadDoesNotAcquireAdjacentForwardCar() {
        var selector = LeadSelector(requiresRearView: true)
        let adjacentID = UUID()
        for index in 0...4 {
            let time = 10 + Double(index) * 0.1
            XCTAssertNil(selector.update([car(id: adjacentID,x: 0.75,at: time)], at: time, road: road(at: time)))
        }
    }

    /// A slot hopping to the neighbouring painted line doubles the corridor width in
    /// one capture. The confirmed corridor is held, not replaced; a repeated new width
    /// (a real lane change) re-confirms in two further captures.
    func testWidthJumpIsHeldLikeAMissAndRepeatedNewWidthReconfirms() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        let lane = try confirm(&estimator)
        let near = lane.bands.first!
        // Both slots land on the outer lines: width 1.45x at every row.
        let wide = [candidate(slot: 1) { y, _ in 0.5 - 1.45 * (0.30 - 0.44 * y) },
                    candidate(slot: 2) { y, _ in 0.5 + 1.45 * (0.30 - 0.44 * y) }]
        XCTAssertNil(estimator.update(wide, excluding: [], at: 10.4))
        XCTAssertTrue(estimator.status.hasPrefix("ego width jumped"))
        XCTAssertTrue(estimator.status.contains("holding"))
        // Back to the original width: measured immediately, no re-confirmation.
        let back = try XCTUnwrap(estimator.update(pair(), excluding: [], at: 10.6))
        XCTAssertEqual(back.bands.first!.right - back.bands.first!.left, near.right - near.left, accuracy: 1e-9)
        // The new width persisting: one hold, then re-confirmation over two captures.
        XCTAssertNil(estimator.update(wide, excluding: [], at: 10.8))
        XCTAssertNil(estimator.update(wide, excluding: [], at: 11.0))
        XCTAssertNil(estimator.update(wide, excluding: [], at: 11.2))
        let accepted = try XCTUnwrap(estimator.update(wide, excluding: [], at: 11.4))
        XCTAssertGreaterThan(accepted.bands.first!.right - accepted.bands.first!.left, 1.35 * (near.right - near.left))
    }

    func testSmallWidthChangesStillMeasureEveryCapture() throws {
        var estimator = UFLDv2EgoCorridorEstimator()
        _ = try confirm(&estimator)
        let slightlyWider = [candidate(slot: 1), candidate(slot: 2) { y, _ in 0.5 + (0.30 - 0.44 * y) * 1.2 }]
        XCTAssertNotNil(estimator.update(slightlyWider, excluding: [], at: 10.4))
        XCTAssertEqual(estimator.status, "measured ego lane confirmed")
    }
}
