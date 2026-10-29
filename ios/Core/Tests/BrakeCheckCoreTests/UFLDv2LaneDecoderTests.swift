import XCTest
@testable import BrakeCheckCore

final class UFLDv2LaneDecoderTests: XCTestCase {
    private typealias Axis = UFLDv2LaneDecoder.Axis
    private func peak(_ axis: Axis, _ bin: Int, _ anchor: Int, _ slot: Int) -> Double {
        let center = axis == .row ? 60 + slot * 30 : 30 + anchor
        return bin == min(98, center) ? 1000 : -1000
    }
    private func innerOnly(_ axis: Axis, _ category: Int, _ anchor: Int, _ slot: Int) -> Double {
        category == 1 && axis == .row && slot == 1 ? 10 : 0
    }

    func testInputTransformMatchesBottomCropAndDoesNotRedefineOutputAnchors() {
        XCTAssertEqual(UFLDv2InputTransform.inputWidth, 1600)
        XCTAssertEqual(UFLDv2InputTransform.inputHeight, 320)
        XCTAssertEqual(UFLDv2InputTransform.resizedHeight, 533)
        XCTAssertEqual(UFLDv2InputTransform.cropOffset, 213)
        XCTAssertEqual(UFLDv2InputTransform.fullFrameTopY(fromInputTopY: 0), 213.0 / 533, accuracy: 1e-12)
        XCTAssertEqual(UFLDv2InputTransform.fullFrameTopY(fromInputTopY: 1), 1, accuracy: 1e-12)
        for y in [0.42, 0.6, 1.0] {
            XCTAssertEqual(UFLDv2InputTransform.fullFrameTopY(fromInputTopY:
                UFLDv2InputTransform.inputTopY(fromFullFrameTopY: y)), y, accuracy: 1e-12)
        }
        XCTAssertEqual(UFLDv2LaneDecoder.rowAnchors.count, 72)
        XCTAssertEqual(UFLDv2LaneDecoder.columnAnchors.count, 81)
        XCTAssertEqual(UFLDv2LaneDecoder.rowAnchors.first!, 0.42, accuracy: 1e-12)
        XCTAssertEqual(UFLDv2LaneDecoder.rowAnchors.last!, 1, accuracy: 1e-12)
    }

    func testTiedExistenceMeansAbsentAndAll72RowsAreReturned() {
        let rows = UFLDv2LaneDecoder.decode(location: { _, _, _, _ in
            XCTFail("Absent anchors must not read locations")
            return 0
        }, existence: { _, _, _, _ in 0 })
        XCTAssertEqual(rows.count, 72)
        XCTAssertTrue(rows.allSatisfy { $0.peaks.isEmpty })
        XCTAssertEqual(rows.first!.y, 0, accuracy: 1e-12)
        XCTAssertEqual(rows.last!.y, 0.58, accuracy: 1e-12)
        XCTAssertTrue(zip(rows, rows.dropFirst()).allSatisfy { $0.y < $1.y })
    }

    func testEachSlotUsesOnlyItsTrainedHead() {
        let candidates = UFLDv2LaneDecoder.decodeCandidates(location: peak,
            existence: { axis, category, _, slot in
                let chosen = (slot == 1 || slot == 2) ? axis == .row : axis == .column
                return category == 1 && chosen ? 10 : 0
            })
        XCTAssertEqual(candidates.map(\.laneSlot), [0, 1, 2, 3])
        XCTAssertEqual(candidates.map(\.axis), [.column, .row, .row, .column])
        let wrongHeads = UFLDv2LaneDecoder.decodeCandidates(location: peak,
            existence: { axis, category, _, slot in
                let wrong = (slot == 1 || slot == 2) ? axis == .column : axis == .row
                return category == 1 && wrong ? 10 : 0
            })
        XCTAssertTrue(wrongHeads.isEmpty)
    }

    func testOriginalRowGateRequires37PositiveAnchors() {
        for count in [36, 37] {
            let candidates = UFLDv2LaneDecoder.decodeCandidates(location: peak,
                existence: { axis, category, anchor, slot in
                    category == 1 && axis == .row && slot == 1 && anchor < count ? 10 : 0
                })
            XCTAssertEqual(candidates.count, count == 37 ? 1 : 0)
            if let candidate = candidates.first { XCTAssertEqual(candidate.positiveAnchors, 37) }
        }
    }

    func testOriginalColumnGateRequires21PositiveAnchors() {
        for count in [20, 21] {
            let candidates = UFLDv2LaneDecoder.decodeCandidates(location: peak,
                existence: { axis, category, anchor, slot in
                    category == 1 && axis == .column && slot == 0 && anchor < count ? 10 : 0
                })
            XCTAssertEqual(candidates.count, count == 21 ? 1 : 0)
        }
    }

    func testAnchorGateCountsExistenceBeforeDiscardingLastImageEdge() throws {
        let candidate = try XCTUnwrap(UFLDv2LaneDecoder.decodeCandidates(location: peak, existence: innerOnly).first)
        XCTAssertEqual(candidate.positiveAnchors, 72)
        XCTAssertEqual(candidate.points.count, 71)
        XCTAssertTrue(candidate.points.allSatisfy { $0.y > 0 && $0.y <= 0.58 + 1e-12 })
    }

    func testMaximumGridHalfBinOutsideImageIsRejected() {
        let candidates = UFLDv2LaneDecoder.decodeCandidates(location: { axis, bin, _, _ in
            bin == (axis == .row ? 199 : 99) ? 1000 : -1000
        }, existence: { _, category, _, _ in category == 1 ? 10 : 0 })
        XCTAssertTrue(candidates.isEmpty)
    }

    func testMinimumGridBinRemainsInsideImage() throws {
        let candidate = try XCTUnwrap(UFLDv2LaneDecoder.decodeCandidates(location: { _, bin, _, _ in
            bin == 0 ? 1000 : -1000
        }, existence: innerOnly).first)
        XCTAssertEqual(candidate.points.first!.x, 0.5 / 199, accuracy: 1e-12)
    }

    func testStableLocalSoftargmaxUsesOnlyWinnerAndAdjacentBins() throws {
        let candidate = try XCTUnwrap(UFLDv2LaneDecoder.decodeCandidates(location: { _, bin, _, _ in
            switch bin {
            case 98: return 999
            case 99: return 1000
            case 100: return 998
            default: return -1000
            }
        }, existence: innerOnly).first)
        let total = exp(-1.0) + 1 + exp(-2.0)
        let expected = ((98 * exp(-1.0) + 99 + 100 * exp(-2.0)) / total + 0.5) / 199
        XCTAssertEqual(candidate.points[0].x, expected, accuracy: 1e-12)
        XCTAssertGreaterThanOrEqual(candidate.points[0].strength, 0.5)
    }

    func testLocationArgmaxTiesKeepTheFirstBinAndItsLocalWindow() throws {
        let candidate = try XCTUnwrap(UFLDv2LaneDecoder.decodeCandidates(location: { _, bin, _, _ in
            bin == 40 || bin == 80 ? 1000 : -1000
        }, existence: innerOnly).first)
        XCTAssertEqual(candidate.points[0].x, 40.5 / 199, accuracy: 1e-12)
    }

    func testFiniteExtremeLogitsDoNotOverflowSoftmaxOrExistence() throws {
        let candidate = try XCTUnwrap(UFLDv2LaneDecoder.decodeCandidates(location: { _, bin, _, _ in
            bin == 100 ? Double.greatestFiniteMagnitude : -Double.greatestFiniteMagnitude
        }, existence: { axis, category, _, slot in
            guard axis == .row && slot == 1 else { return 0 }
            return category == 1 ? Double.greatestFiniteMagnitude : -Double.greatestFiniteMagnitude
        }).first)
        XCTAssertEqual(candidate.points[0].x, 100.5 / 199, accuracy: 1e-12)
        XCTAssertEqual(candidate.points[0].strength, 1)
    }

    func testInvalidExistenceAndLocationLogitsFailClosed() {
        for invalid in [Double.nan, Double.infinity, -Double.infinity] {
            XCTAssertTrue(UFLDv2LaneDecoder.decodeCandidates(location: peak,
                existence: { _, category, _, _ in category == 1 ? invalid : 0 }).isEmpty)
            XCTAssertTrue(UFLDv2LaneDecoder.decodeCandidates(location: { _, _, _, _ in invalid },
                existence: innerOnly).isEmpty)
        }
        let partial = UFLDv2LaneDecoder.decodeCandidates(location: { axis, bin, anchor, slot in
            anchor == 20 ? .nan : self.peak(axis, bin, anchor, slot)
        }, existence: innerOnly)
        XCTAssertEqual(partial.first?.positiveAnchors, 72)
        XCTAssertEqual(partial.first?.points.count, 70)
    }

    func testRowAnchorsFlipYAndNeverInterpolateMissingSamplesOrEndpoints() {
        let rows = UFLDv2LaneDecoder.decode(location: peak, existence: { axis, category, anchor, slot in
            category == 1 && axis == .row && slot == 1 && anchor < 38 && anchor != 20 ? 10 : 0
        })
        XCTAssertEqual(rows.filter { !$0.peaks.isEmpty }.count, 37)
        let missingY = 1 - UFLDv2LaneDecoder.rowAnchors[20]
        XCTAssertTrue(rows.first { abs($0.y - missingY) < 1e-12 }!.peaks.isEmpty)
        XCTAssertTrue(rows.filter { $0.y < 1 - UFLDv2LaneDecoder.rowAnchors[37] }.allSatisfy { $0.peaks.isEmpty })
        XCTAssertEqual(rows.last!.peaks.first!.x, 90.5 / 199, accuracy: 1e-12)
    }

    func testColumnPolylinesResampleOnlyWithinObservedEndpoints() throws {
        let rows = UFLDv2LaneDecoder.decode(location: peak,
            existence: { axis, category, anchor, slot in
                category == 1 && axis == .column && slot == 0 && (20...40).contains(anchor) ? 10 : 0
            })
        let lower = 1 - 70.5 / 99
        let upper = 1 - 50.5 / 99
        XCTAssertTrue(rows.contains { !$0.peaks.isEmpty })
        for row in rows {
            if row.y < lower || row.y > upper { XCTAssertTrue(row.peaks.isEmpty) }
            for peak in row.peaks {
                XCTAssertEqual(peak.x, (99 * (1 - row.y) - 30.5) / 80, accuracy: 1e-10)
            }
        }
    }

    func testColumnCurveWithMultipleCrossingsIsRejectedRatherThanPickingOneBranch() {
        let rows = UFLDv2LaneDecoder.decode(location: { _, bin, anchor, _ in
            let center = anchor <= 40 ? 20 + anchor : 100 - anchor
            return bin == center ? 1000 : -1000
        }, existence: { axis, category, anchor, slot in
            category == 1 && axis == .column && slot == 0 && (10...70).contains(anchor) ? 10 : 0
        })
        XCTAssertTrue(rows.allSatisfy { $0.peaks.isEmpty })
    }

    func testHorizontalColumnTraceDoesNotInventVerticalExtent() {
        let rows = UFLDv2LaneDecoder.decode(location: { _, bin, _, _ in bin == 50 ? 1000 : -1000 },
            existence: { axis, category, _, slot in category == 1 && axis == .column && slot == 0 ? 10 : 0 })
        XCTAssertTrue(rows.allSatisfy { $0.peaks.isEmpty })
    }

    func testEgoOnlyKeepsTrainedInnerSlotsAndDropsNeighboringOuterSlots() {
        let existence: (Axis, Int, Int, Int) -> Double = { _, category, _, _ in category == 1 ? 10 : 0 }
        let all = UFLDv2LaneDecoder.decode(location: peak, existence: existence)
        let ego = UFLDv2LaneDecoder.decode(egoOnly: true, location: peak, existence: existence)
        XCTAssertTrue(all.contains { $0.peaks.count > 2 })
        XCTAssertTrue(ego.allSatisfy { $0.peaks.count <= 2 })
        XCTAssertTrue(ego.contains { $0.peaks.count == 2 })
        for row in ego {
            for peak in row.peaks {
                XCTAssertTrue(abs(peak.x - 90.5 / 199) < 1e-12 || abs(peak.x - 98.5 / 199) < 1e-12)
            }
        }
    }

    func testEgoOnlySkipsOuterHeadsBeforeReadingAnyTensorValues() {
        var locationReads = 0
        var existenceReads = 0
        let rows = UFLDv2LaneDecoder.decode(egoOnly: true, location: { axis, bin, anchor, slot in
            XCTAssertEqual(axis, .row)
            XCTAssertTrue(slot == 1 || slot == 2)
            locationReads += 1
            return self.peak(axis, bin, anchor, slot)
        }, existence: { axis, category, _, slot in
            XCTAssertEqual(axis, .row)
            XCTAssertTrue(slot == 1 || slot == 2)
            existenceReads += 1
            return category == 1 ? 10 : 0
        })
        XCTAssertEqual(locationReads, 2 * 72 * 200)
        XCTAssertEqual(existenceReads, 2 * 72 * 2)
        XCTAssertTrue(rows.contains { $0.peaks.count == 2 })
    }

    func testVehicleExclusionUsesBottomLeftCoordinatesAndEightThousandthsMargin() {
        let x = 90.5 / 199
        let y = 1 - UFLDv2LaneDecoder.rowAnchors[30]
        let box = CGRect(x: x + 0.007, y: y - 0.001, width: 0.001, height: 0.002)
        let rows = UFLDv2LaneDecoder.decode(excluding: [box], location: peak, existence: innerOnly)
        XCTAssertTrue(rows.first { abs($0.y - y) < 1e-12 }!.peaks.isEmpty)
        XCTAssertFalse(rows.last!.peaks.isEmpty)
    }

    func testFrozenValidationLogitsMatchIndependentPythonDecoder() throws {
        struct Fixture: Decodable {
            struct Head: Decodable {
                struct Anchor: Decodable {
                    let existence: [Double]
                    let center: Int
                    let start: Int
                    let local: [Double]
                }
                let axis: String
                let slot: Int
                let anchors: [Anchor]
            }
            struct Expected: Decodable {
                let slot: Int
                let positiveAnchors: Int
                let probability: Double
                let points: [[Double]]
            }
            let image_id: String
            let heads: [Head]
            let expected: [Expected]
        }
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Fixtures/UFLDv2DecoderAudit.json")
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: fixtureURL))
        XCTAssertEqual(fixture.image_id, "a2d2-a101a86582d2561a")
        let candidates = UFLDv2LaneDecoder.decodeCandidates(location: { axis, bin, anchor, slot in
            let head = fixture.heads[slot]
            XCTAssertEqual(axis, head.axis == "row" ? .row : .column)
            let entry = head.anchors[anchor]
            let index = bin - entry.start
            return entry.local.indices.contains(index) ? entry.local[index] : -10000
        }, existence: { _, category, anchor, slot in fixture.heads[slot].anchors[anchor].existence[category] })
        XCTAssertEqual(candidates.count, 4)
        for (candidate, expected) in zip(candidates, fixture.expected) {
            XCTAssertEqual(candidate.laneSlot, expected.slot)
            XCTAssertEqual(candidate.positiveAnchors, expected.positiveAnchors)
            XCTAssertEqual(candidate.meanExistenceProbability, expected.probability, accuracy: 2e-7)
            XCTAssertEqual(candidate.points.count, expected.points.count)
            for (point, expectedPoint) in zip(candidate.points, expected.points) {
                // Audit arithmetic uses float32 local softmax. Swift's stable
                // Double computation agrees within well under one pixel.
                XCTAssertEqual(point.x, expectedPoint[0], accuracy: 2e-7)
                XCTAssertEqual(point.y, expectedPoint[1], accuracy: 2e-7)
            }
        }
    }
}