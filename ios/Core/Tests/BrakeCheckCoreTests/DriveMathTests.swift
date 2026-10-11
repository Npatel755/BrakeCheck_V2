import XCTest
@testable import BrakeCheckCore

final class DriveMathTests: XCTestCase {
    func testClosingCarHasLowerAbsoluteSpeedAndFiniteTTC() {
        let result = RiskRules.evaluate(distance: 20, egoSpeed: 25, rangeRate: -10, leadAcceleration: 0)
        XCTAssertEqual(result.leadSpeed, 15)
        XCTAssertEqual(result.closingSpeed, 10)
        XCTAssertEqual(result.timeToCollision, 2)
        XCTAssertTrue([RiskLevel.high, .critical].contains(result.level))
    }

    func testRecedingCarDoesNotHaveCollisionTime() {
        let result = RiskRules.evaluate(distance: 80, egoSpeed: 20, rangeRate: 5, leadAcceleration: 0)
        XCTAssertEqual(result.leadSpeed, 25)
        XCTAssertEqual(result.closingSpeed, 0)
        XCTAssertNil(result.timeToCollision)
        XCTAssertEqual(result.level, .low)
    }

    func testCloseFollowingStillRaisesRiskWithoutClosingSpeed() {
        let result = RiskRules.evaluate(distance: 10, egoSpeed: 25, rangeRate: 0, leadAcceleration: 0)
        XCTAssertNil(result.timeToCollision)
        XCTAssertEqual(result.headway, 0.4)
        XCTAssertEqual(result.level, .critical)
    }

    func testBothCarsParkedDoesNotDivideByZero() {
        let result = RiskRules.evaluate(distance: 5, egoSpeed: 0, rangeRate: 0, leadAcceleration: nil)
        XCTAssertNil(result.headway)
        XCTAssertNil(result.timeToCollision)
        XCTAssertEqual(result.level, .low)
    }

    func testStrongLeadBrakingRaisesRisk() {
        let result = RiskRules.evaluate(distance: 50, egoSpeed: 25, rangeRate: 0, leadAcceleration: -6)
        XCTAssertEqual(result.level, .critical)
    }

    func testInvalidInputsAreUnknownInsteadOfLowRisk() {
        for gap in [-1.0, 0, Double.nan, Double.infinity] {
            let result = RiskRules.evaluate(distance: gap, egoSpeed: 25, rangeRate: -1, leadAcceleration: nil)
            XCTAssertEqual(result.level, .unknown)
            XCTAssertNil(result.score)
        }
    }

    func testInconsistentNegativeLeadSpeedIsRejected() {
        let result = RiskRules.evaluate(distance: 20, egoSpeed: 2, rangeRate: -10, leadAcceleration: nil)
        XCTAssertEqual(result.level, .unknown)
    }

    func testBoxDoublingHalvesEstimatedDistance() {
        XCTAssertEqual(DriveMath.calibratedDistance(referenceMeters: 20, referenceWidth: 0.1, currentWidth: 0.2), 10)
        XCTAssertNil(DriveMath.calibratedDistance(referenceMeters: 20, referenceWidth: 0.1, currentWidth: 0))
    }

    func testSlopeWithLargeTimestamps() {
        let points = (0...10).map { i in TimedValue(time: 900_000_000 + Double(i), value: 80 - 3 * Double(i)) }
        XCTAssertEqual(DriveMath.slope(points, minimumSpan: 1)!, -3, accuracy: 1e-9)
    }

    func testFreshStableTrackingProducesCorrectVelocity() {
        var estimator = RiskEstimator()
        var result = RiskSnapshot()
        for i in 0...30 {
            let t = Double(i) / 10
            result = estimator.update(time: t, distance: 50 - 5 * t, egoSpeed: 25, gpsAge: 0.2, confidence: 0.9)
        }
        XCTAssertEqual(result.leadSpeed!, 20, accuracy: 0.01)
        XCTAssertEqual(result.closingSpeed!, 5, accuracy: 0.01)
        XCTAssertEqual(result.leadAcceleration!, 0, accuracy: 0.01)
    }

    func testStaleGPSImmediatelyInvalidatesEstimate() {
        var estimator = warmedEstimator()
        let result = estimator.update(time: 3.1, distance: 70, egoSpeed: 25, gpsAge: 2.1, confidence: 0.9)
        XCTAssertEqual(result.level, .unknown)
        XCTAssertNil(result.leadSpeed)
    }

    func testLowConfidenceDoesNotReusePreviousEstimate() {
        var estimator = warmedEstimator()
        let result = estimator.update(time: 3.1, distance: 70, egoSpeed: 25, gpsAge: 0.1, confidence: 0.2)
        XCTAssertEqual(result.level, .unknown)
    }

    func testNewTargetRequiresWarmup() {
        var estimator = warmedEstimator()
        estimator.reset()
        let result = estimator.update(time: 5, distance: 30, egoSpeed: 25, gpsAge: 0.1, confidence: 0.9)
        XCTAssertNil(result.leadSpeed)
        XCTAssertEqual(result.level, .unknown)
    }

    func testGapInCameraSamplesResetsDerivative() {
        var estimator = warmedEstimator()
        let result = estimator.update(time: 6, distance: 70, egoSpeed: 25, gpsAge: 0.1, confidence: 0.9)
        XCTAssertEqual(result.level, .unknown)
    }

    func testAbruptBoxJumpIsRejected() {
        var estimator = warmedEstimator()
        let result = estimator.update(time: 3.1, distance: 10, egoSpeed: 25, gpsAge: 0.1, confidence: 0.9)
        XCTAssertEqual(result.level, .unknown)
    }

    private func warmedEstimator() -> RiskEstimator {
        var estimator = RiskEstimator()
        for i in 0...30 {
            _ = estimator.update(time: Double(i) / 10, distance: 70, egoSpeed: 25, gpsAge: 0.1, confidence: 0.9)
        }
        return estimator
    }
}
