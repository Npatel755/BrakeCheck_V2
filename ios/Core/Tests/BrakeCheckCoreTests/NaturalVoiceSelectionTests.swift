import XCTest
@testable import BrakeCheckCore

final class NaturalVoiceSelectionTests: XCTestCase {
    func testAutomaticSelectionAvoidsAlexWhenFemaleVoiceAvailable() {
        XCTAssertTrue(NaturalVoiceSelection.prefers(SpeechVoiceCandidate(name: "Samantha", quality: 1, isFemale: true),
            over: SpeechVoiceCandidate(name: "Alex", quality: 3, isFemale: false)))
    }
    func testBestFemaleQualityWinsBeforePreferredName() {
        XCTAssertTrue(NaturalVoiceSelection.prefers(SpeechVoiceCandidate(name: "Samantha", quality: 2, isFemale: true),
            over: SpeechVoiceCandidate(name: "Ava", quality: 1, isFemale: true)))
    }
    func testKnownNaturalNameCanHandleUnspecifiedGenderMetadata() {
        XCTAssertTrue(NaturalVoiceSelection.prefers(SpeechVoiceCandidate(name: "Ava", quality: 1, isFemale: false),
            over: SpeechVoiceCandidate(name: "Alex", quality: 3, isFemale: false)))
    }
    func testFallbackStillRanksQualityWhenOnlyMaleVoicesExist() {
        XCTAssertTrue(NaturalVoiceSelection.prefers(SpeechVoiceCandidate(name: "Daniel", quality: 2, isFemale: false),
            over: SpeechVoiceCandidate(name: "Alex", quality: 1, isFemale: false)))
    }
}
