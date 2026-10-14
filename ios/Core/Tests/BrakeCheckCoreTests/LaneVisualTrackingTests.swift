import XCTest
@testable import BrakeCheckCore

final class LaneVisualTrackingTests: XCTestCase {
    private func fixture() -> (LanePath, LaneLumaFrame, LaneLumaFrame) {
        let width = 320, height = 180
        var seed: UInt32 = 4711
        let pixels = (0..<(width * height)).map { _ -> UInt8 in
            seed = seed &* 1664525 &+ 1013904223
            return UInt8(truncatingIfNeeded: seed >> 24)
        }
        var moved = [UInt8](repeating: 0, count: pixels.count)
        for y in 2..<height { for x in 0..<(width - 5) {
            moved[(y - 2) * width + x + 5] = pixels[y * width + x]
        } }
        let bands = (0..<24).map { index -> LaneBand in
            let y = 0.12 + Double(index) * 0.018
            let half = 0.35 * (0.7 - y)
            return LaneBand(y: y, left: 0.5 - half, right: 0.5 + half)
        }
        return (LanePath(bands: bands, confidence: 0.95, capturedAt: 10, source: .neural),
            LaneLumaFrame(width: width, height: height, pixels: pixels, capturedAt: 10)!,
            LaneLumaFrame(width: width, height: height, pixels: moved, capturedAt: 10.15)!)
    }
    func testSparseSearchVerifiesCorrectMotionWithFullPatches() throws {
        let (lane, original, current) = fixture()
        let tracked = try XCTUnwrap(LaneVisualTracker.project(lane, from: original, to: current))
        let newY = 0.35
        let old = lane.bounds(at: newY - 2.0 / 179)!
        let actual = tracked.bounds(at: newY)!
        XCTAssertEqual(actual.left, old.left + 5.0 / 319, accuracy: 0.001)
        XCTAssertEqual(actual.right, old.right + 5.0 / 319, accuracy: 0.001)
        XCTAssertEqual(tracked.capturedAt, current.capturedAt)
    }
    func testUniformFramesNeverInventTrackableTexture() {
        let (lane, _, _) = fixture()
        let pixels = [UInt8](repeating: 120, count: 320 * 180)
        let a = LaneLumaFrame(width: 320, height: 180, pixels: pixels, capturedAt: 10)!
        let b = LaneLumaFrame(width: 320, height: 180, pixels: pixels, capturedAt: 10.15)!
        XCTAssertNil(LaneVisualTracker.project(lane, from: a, to: b))
    }
    func testForegroundOcclusionStillRejectsPatchMatches() {
        let (lane, a, b) = fixture()
        XCTAssertNil(LaneVisualTracker.project(lane, from: a, to: b,
            excluding: [CGRect(x: 0, y: 0, width: 1, height: 1)]))
    }
    func testColdStartLatencyDoesNotPermanentlyDisableWarmModels() {
        var history = AuxiliaryLatencyHistory()
        history.observe(3)
        history.observe(0.4)
        XCTAssertEqual(history.seconds, 0)
        history.observe(0.05)
        let policy = ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal,
            vehicleLatency: 0.04, laneLatency: history.seconds, roadLatency: history.seconds)
        XCTAssertTrue(policy.laneAllowed)
        XCTAssertNotNil(policy.roadInterval)
        history.observe(0.5)
        XCTAssertGreaterThan(history.seconds, 0.15)
    }
}
