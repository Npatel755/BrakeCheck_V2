import XCTest
@testable import BrakeCheckCore

final class DetectorLightingTests: XCTestCase {
    private let dark = DetectorLightingSample(medianLuma: 0.08, darkFraction: 0.85)
    private let bright = DetectorLightingSample(medianLuma: 0.65, darkFraction: 0.08)
    private let neutral = DetectorLightingSample(medianLuma: 0.26, darkFraction: 0.40)

    private func feed(_ sample: DetectorLightingSample, from start: Double, through end: Double,
                      into selector: inout DetectorLightingSelector) {
        for t in stride(from: start, through: end, by: 0.5) { selector.update(sample, at: t) }
    }

    func testSustainedDarknessSelectsNightAfterConfirmation() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 0, through: 2, into: &selector)
        XCTAssertEqual(selector.profile, .day)
        selector.update(dark, at: 2.5)
        XCTAssertEqual(selector.profile, .night)
    }

    func testBriefDarkFlashDoesNotSwitch() {
        var selector = DetectorLightingSelector()
        selector.update(dark, at: 0)
        feed(bright, from: 0.5, through: 8, into: &selector)
        XCTAssertEqual(selector.profile, .day)
    }

    func testBrightFlashDoesNotReleaseNightMode() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 0, through: 3, into: &selector)
        selector.update(bright, at: 3.5)
        feed(dark, from: 4, through: 10, into: &selector)
        XCTAssertEqual(selector.profile, .night)
    }

    func testNeutralBandKeepsCurrentProfile() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 0, through: 3, into: &selector)
        feed(neutral, from: 3.5, through: 14, into: &selector)
        XCTAssertEqual(selector.profile, .night)
    }

    func testReturningDaylightNeedsDwellAndSustainedEvidence() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 0, through: 2.5, into: &selector)
        feed(bright, from: 3, through: 8, into: &selector)
        XCTAssertEqual(selector.profile, .night)
        selector.update(bright, at: 8.5)
        XCTAssertEqual(selector.profile, .day)
    }

    func testManualSelectionOverridesLightingImmediately() {
        var selector = DetectorLightingSelector()
        selector.setMode(.night)
        XCTAssertEqual(selector.profile, .night)
        feed(bright, from: 0, through: 10, into: &selector)
        XCTAssertEqual(selector.profile, .night)
        selector.setMode(.day)
        XCTAssertEqual(selector.profile, .day)
        feed(dark, from: 10.5, through: 20, into: &selector)
        XCTAssertEqual(selector.profile, .day)
    }

    func testReturningToAutoKeepsProfileUntilFreshEvidence() {
        var selector = DetectorLightingSelector()
        selector.setMode(.night)
        selector.setMode(.automatic)
        XCTAssertEqual(selector.profile, .night)
        feed(bright, from: 0, through: 2, into: &selector)
        XCTAssertEqual(selector.profile, .night)
        selector.update(bright, at: 2.5)
        XCTAssertEqual(selector.profile, .day)
    }

    func testMissingFramesCannotCompleteAConfirmation() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 0, through: 2, into: &selector)
        selector.update(dark, at: 10)
        XCTAssertEqual(selector.profile, .day)
        feed(dark, from: 10.5, through: 12, into: &selector)
        XCTAssertEqual(selector.profile, .day)
        selector.update(dark, at: 12.5)
        XCTAssertEqual(selector.profile, .night)
    }

    func testInvalidSampleBreaksPendingConfirmation() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 0, through: 2, into: &selector)
        selector.update(DetectorLightingSample(medianLuma: .nan, darkFraction: 0.9), at: 2.5)
        feed(dark, from: 3, through: 5, into: &selector)
        XCTAssertEqual(selector.profile, .day)
        selector.update(dark, at: 5.5)
        XCTAssertEqual(selector.profile, .night)
    }

    func testDuplicateSamplesDoNotCountAsDistinctObservations() {
        var selector = DetectorLightingSelector()
        for _ in 0..<30 { selector.update(dark, at: 0) }
        XCTAssertEqual(selector.profile, .day)
        XCTAssertFalse(selector.shouldSample(at: 0.1))
        XCTAssertTrue(selector.shouldSample(at: 0.5))
    }

    func testBackwardsClockRequiresNewConfirmation() {
        var selector = DetectorLightingSelector()
        feed(dark, from: 10, through: 12, into: &selector)
        selector.update(dark, at: 0)
        feed(dark, from: 0.5, through: 2, into: &selector)
        XCTAssertEqual(selector.profile, .day)
        selector.update(dark, at: 2.5)
        XCTAssertEqual(selector.profile, .night)
    }

    func testExposureDetectsAnAutoBrightenedDarkScene() {
        var selector = DetectorLightingSelector()
        let sample = DetectorLightingSample(medianLuma: 0.36, darkFraction: 0.30,
            iso: 1200, exposureSeconds: 1.0 / 30)
        feed(sample, from: 0, through: 3, into: &selector)
        XCTAssertEqual(selector.profile, .night)
    }

    func testHighISOWithShortExposureDoesNotForceNight() {
        var selector = DetectorLightingSelector()
        let sample = DetectorLightingSample(medianLuma: 0.36, darkFraction: 0.30,
            iso: 1200, exposureSeconds: 1.0 / 500)
        feed(sample, from: 0, through: 6, into: &selector)
        XCTAssertEqual(selector.profile, .day)
    }

    func testInvalidExposureAndLumaValuesAreRejected() {
        XCTAssertFalse(DetectorLightingSample(medianLuma: .infinity, darkFraction: 0.5).isUsable)
        XCTAssertFalse(DetectorLightingSample(medianLuma: 0.2, darkFraction: 1.1).isUsable)
        XCTAssertFalse(DetectorLightingSample(medianLuma: 0.2, darkFraction: 0.5, iso: -1).isUsable)
        XCTAssertFalse(DetectorLightingSample(medianLuma: 0.2, darkFraction: 0.5, exposureSeconds: .nan).isUsable)
    }
}
