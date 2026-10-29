import Foundation
import XCTest
@testable import BrakeCheckCore

final class RecordingTimelineTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1000.125)

    func testSingleFrameHasItsOwnDurationAndSurvivesFinalization() throws {
        let timeline = try XCTUnwrap(RecordingSegmentTimeline(presentationTime: 200,
            capturedAt: start, frameDuration: 1.0 / 24))
        let segment = timeline.segment(filename: "one-frame.mp4")
        XCTAssertTrue(segment.valid)
        XCTAssertEqual(segment.end.timeIntervalSince(segment.start), 1.0 / 24, accuracy: 0.000001)
        XCTAssertEqual(timeline.endPresentationTime, 200 + 1.0 / 24, accuracy: 0.000001)
    }

    func testAcceptedFrameTimelineUsesCaptureTimeAndLastFrameDuration() throws {
        var timeline = try XCTUnwrap(RecordingSegmentTimeline(presentationTime: 200,
            capturedAt: start, frameDuration: 1.0 / 30))
        XCTAssertTrue(timeline.append(presentationTime: 200.1, capturedAt: start.addingTimeInterval(0.1), frameDuration: 1.0 / 24))
        XCTAssertEqual(timeline.start, start)
        XCTAssertEqual(timeline.end.timeIntervalSince(start), 0.1 + 1.0 / 24, accuracy: 0.000001)
        XCTAssertFalse(timeline.append(presentationTime: 200.1, capturedAt: start.addingTimeInterval(0.1), frameDuration: 1))
        XCTAssertEqual(timeline.lastFrameDuration, 1.0 / 24)
    }

    func testLongFrameGapsAndCameraClockResetsSplitInsteadOfInventingContinuity() throws {
        let timeline = try XCTUnwrap(RecordingSegmentTimeline(presentationTime: 200,
            capturedAt: start, frameDuration: nil))
        XCTAssertEqual(timeline.decision(presentationTime: 201.1, capturedAt: start.addingTimeInterval(1.1)), .split)
        XCTAssertEqual(timeline.decision(presentationTime: 1, capturedAt: start.addingTimeInterval(1)), .split)
        XCTAssertEqual(timeline.decision(presentationTime: 200, capturedAt: start), .skip)
        XCTAssertEqual(timeline.decision(presentationTime: 200.5, capturedAt: start.addingTimeInterval(10)), .split)
    }

    func testRolloverIsMeasuredFromAcceptedFrames() throws {
        var timeline = try XCTUnwrap(RecordingSegmentTimeline(presentationTime: 200,
            capturedAt: start, frameDuration: nil))
        for index in 1..<10 {
            let offset = Double(index)
            XCTAssertTrue(timeline.append(presentationTime: 200 + offset, capturedAt: start.addingTimeInterval(offset), frameDuration: nil))
        }
        XCTAssertEqual(timeline.decision(presentationTime: 210, capturedAt: start.addingTimeInterval(10)), .split)
        XCTAssertEqual(timeline.end.timeIntervalSince(start), 9 + 1.0 / 30, accuracy: 0.000001)
    }

    func testInvalidSampleTimesAreRejectedAndInvalidDurationsUseNominalFrameLength() throws {
        for invalid in [Double.nan, .infinity, -.infinity, -1] {
            XCTAssertNil(RecordingSegmentTimeline(presentationTime: invalid, capturedAt: start, frameDuration: nil))
        }
        for duration: Double? in [nil, .nan, .infinity, -1, 0, 10] {
            let timeline = try XCTUnwrap(RecordingSegmentTimeline(presentationTime: 200, capturedAt: start, frameDuration: duration))
            XCTAssertEqual(timeline.lastFrameDuration, 1.0 / 30)
            XCTAssertEqual(timeline.decision(presentationTime: .nan, capturedAt: start), .skip)
            XCTAssertEqual(timeline.decision(presentationTime: 201, capturedAt: Date(timeIntervalSince1970: .nan)), .skip)
        }
    }

    func testCaptureDateSubtractsDeliveryDelayAndDoesNotLoosenInferenceFreshness() throws {
        let recorded = try XCTUnwrap(CaptureFrameTiming.captureUptime(frameHostTime: 100,
            hostNow: 101.2, uptimeNow: 501.2, maximumAge: 3))
        XCTAssertEqual(recorded, 500, accuracy: 0.000001)
        let date = try XCTUnwrap(CaptureFrameTiming.captureDate(capturedAt: recorded, uptimeNow: 501.2,
            wallNow: start.addingTimeInterval(1.2)))
        XCTAssertEqual(date.timeIntervalSince(start), 0, accuracy: 0.000001)
        XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 100, hostNow: 101.2, uptimeNow: 501.2))
        XCTAssertFalse(TrackingFreshness.measurementIsFresh(capturedAt: recorded, now: 501.2))
        XCTAssertNil(CaptureFrameTiming.captureDate(capturedAt: 502, uptimeNow: 501, wallNow: start))
        XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 100, hostNow: 100, uptimeNow: 500, maximumAge: .nan))
    }

    func testFractionalRecordingMetadataRoundTripPreservesReplayAlignment() throws {
        var event = IncidentManifest(at: start, reason: "Test")
        event.segments = [VideoSegment(filename: "clip.mp4", start: start.addingTimeInterval(-0.025),
                                      end: start.addingTimeInterval(1.875))]
        let data = try RecordingMetadataCodec.encoder().encode(event)
        let restored = try RecordingMetadataCodec.decoder().decode(IncidentManifest.self, from: data)
        XCTAssertEqual(restored.triggerTime.timeIntervalSince(event.triggerTime), 0, accuracy: 0.001)
        XCTAssertEqual(restored.segments[0].start.timeIntervalSince(event.segments[0].start), 0, accuracy: 0.001)
        XCTAssertEqual(restored.segments[0].end.timeIntervalSince(event.segments[0].end), 0, accuracy: 0.001)
        XCTAssertTrue(String(decoding: data, as: UTF8.self).contains(".125Z"))
    }

    func testLegacyWholeSecondMetadataStillLoadsAndInvalidDatesAreRejected() throws {
        let segment = VideoSegment(filename: "clip.mp4", start: Date(timeIntervalSince1970: 1000),
                                   end: Date(timeIntervalSince1970: 1001))
        let legacy = JSONEncoder(); legacy.dateEncodingStrategy = .iso8601
        XCTAssertEqual(try RecordingMetadataCodec.decoder().decode(VideoSegment.self, from: legacy.encode(segment)), segment)
        let invalid = Data("{\"filename\":\"clip.mp4\",\"start\":\"invalid\",\"end\":\"1970-01-01T00:00:00Z\"}".utf8)
        XCTAssertThrowsError(try RecordingMetadataCodec.decoder().decode(VideoSegment.self, from: invalid))
    }
}
