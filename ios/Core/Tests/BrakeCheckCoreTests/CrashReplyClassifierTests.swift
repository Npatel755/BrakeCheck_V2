import XCTest
@testable import BrakeCheckCore

final class CrashReplyClassifierTests: XCTestCase {
    func testClearEnglishAndSpanishAffirmatives() {
        for transcript in ["I'm okay", "I am fine", "I feel fine", "I'm all right.",
                           "Estoy bien", "Sí, estoy bien."] {
            XCTAssertEqual(CrashReplyClassifier.classify(transcript), .ok, transcript)
        }
    }

    func testHelpWinsOverAffirmativesAndNegatedAnswers() {
        for transcript in ["I'm not okay", "I don't feel fine", "I am not sure, I'm okay",
                           "I'm okay, actually help", "I need an ambulance", "No estoy bien",
                           "Sí, pero necesito ayuda"] {
            XCTAssertEqual(CrashReplyClassifier.classify(transcript), .help, transcript)
        }
    }

    func testSilenceAndUnclearSpeechCannotCancelCheckin() {
        for transcript in ["", "  ", "maybe", "I can't tell", "the car stopped",
                           "yes", "good", "okay", "all right", "si", "say I'm okay",
                           "I think I'm okay", "he said I am fine", "I'm okay maybe"] {
            XCTAssertEqual(CrashReplyClassifier.classify(transcript), .unrecognized, transcript)
        }
    }

    func testKeywordsRequireWholeWords() {
        XCTAssertEqual(CrashReplyClassifier.classify("nothing"), .unrecognized)
        XCTAssertEqual(CrashReplyClassifier.classify("yesterday"), .unrecognized)
        XCTAssertEqual(CrashReplyClassifier.classify("I'm good"), .unrecognized)
        XCTAssertEqual(CrashReplyClassifier.classify("I am injured"), .help)
    }
}
