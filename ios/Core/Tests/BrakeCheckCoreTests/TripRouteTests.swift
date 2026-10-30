import Foundation
import XCTest
@testable import BrakeCheckCore

final class TripRouteTests: XCTestCase {
    private func point(_ latitude: Double = 33, longitude: Double = -84, time: Double) -> TripRoute.Point {
        .init(latitude: latitude, longitude: longitude, time: Date(timeIntervalSince1970: time))
    }
    func testRecorderRejectsInvalidAndOutOfOrderFixesWithoutPoisoningNextFix() {
        var recorder = TripRouteAccumulator()
        XCTAssertTrue(recorder.add(point(time: 100)))
        XCTAssertFalse(recorder.add(point(.nan, time: 1_000)))
        XCTAssertFalse(recorder.add(point(34, time: 90)))
        XCTAssertFalse(recorder.add(point(33.001, time: 103)))
        XCTAssertTrue(recorder.add(point(33.001, time: 105)))
        XCTAssertEqual(recorder.route.points.count, 2); XCTAssertTrue(recorder.route.valid)
    }
    func testFutureLocationDoesNotPlaceEventAndInvalidReadingsStayUnavailable() {
        var recorder = TripRouteAccumulator()
        XCTAssertTrue(recorder.add(.brakeAlert, at: Date(timeIntervalSince1970: 100),
            location: point(time: 101), speedMPH: .infinity, closingSeconds: -.infinity))
        let event = recorder.route.events[0]
        XCTAssertNil(event.latitude); XCTAssertNil(event.longitude)
        XCTAssertNil(event.speedMPH); XCTAssertNil(event.closingSeconds)
        XCTAssertTrue(recorder.route.valid)
    }
    func testStaleLocationIsOmittedAndRepeatedEventNeedsSpacing() {
        var recorder = TripRouteAccumulator()
        XCTAssertTrue(recorder.add(.hardBrake, at: Date(timeIntervalSince1970: 100),
            location: point(time: 84), speedMPH: 20, closingSeconds: 4))
        XCTAssertNil(recorder.route.events[0].latitude)
        XCTAssertFalse(recorder.add(.hardBrake, at: Date(timeIntervalSince1970: 107),
            location: nil, speedMPH: nil, closingSeconds: nil))
        XCTAssertTrue(recorder.add(.hardBrake, at: Date(timeIntervalSince1970: 108),
            location: point(time: 108), speedMPH: nil, closingSeconds: nil))
    }
    func testGPSGapsAndImpossibleJumpsAreNotDrawnOrCountedAsDriving() throws {
        let route = TripRoute(points: [point(time: 100), point(33.001, time: 105),
            point(34, time: 200), point(34.001, time: 205), point(40, time: 210)])
        XCTAssertEqual(route.continuousSegments.map(\.count), [2, 2, 1])
        XCTAssertEqual(try XCTUnwrap(route.distanceMeters), 222.39, accuracy: 1)
    }
    func testDatelineCrossingIsARealShortSegment() throws {
        let route = TripRoute(points: [point(0, longitude: 179.999, time: 100),
            point(0, longitude: -179.999, time: 105)])
        XCTAssertEqual(route.continuousSegments.count, 1)
        XCTAssertEqual(try XCTUnwrap(route.distanceMeters), 222.39, accuracy: 1)
    }
    func testInvalidArchiveCannotCreateMapGeometry() {
        let route = TripRoute(points: [point(time: 100), point(99, time: 105)])
        XCTAssertFalse(route.valid); XCTAssertTrue(route.continuousSegments.isEmpty); XCTAssertNil(route.distanceMeters)
        let reversed = TripRoute(points: [point(time: 100), point(33.1, time: 99)])
        XCTAssertFalse(reversed.valid)
    }
    func testExplicitTripIdentityWinsOverClockTimeButLegacyKeepsBoundaries() {
        let start = Date(timeIntervalSince1970: 1_000.123), end = Date(timeIntervalSince1970: 1_001.789)
        let trip = TripMetrics(startedAt: start, endedAt: end, maxSpeedMPH: nil,
            estimatedLeadHardBrakes: 0, estimatedCloseCalls: 0)
        XCTAssertTrue(trip.includes(IncidentManifest(at: start.addingTimeInterval(-5_000), reason: "clock jump", tripID: trip.id)))
        XCTAssertFalse(trip.includes(IncidentManifest(at: start, reason: "different trip", tripID: UUID())))
        XCTAssertTrue(trip.includes(IncidentManifest(at: start, reason: "legacy")))
        XCTAssertTrue(trip.includes(IncidentManifest(at: end, reason: "legacy")))
        XCTAssertFalse(trip.includes(IncidentManifest(at: end.addingTimeInterval(0.001), reason: "legacy")))
    }
    func testLegacyWholeSecondRouteAndNewFractionalTimesBothDecode() throws {
        let route = TripRoute(points: [point(time: 1_000.123)])
        let data = try RecordingMetadataCodec.encoder().encode(route)
        let current = try RecordingMetadataCodec.decoder().decode(TripRoute.self, from: data)
        XCTAssertEqual(current.points[0].time.timeIntervalSince1970, 1_000.123, accuracy: 0.001)
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        let legacy = try RecordingMetadataCodec.decoder().decode(TripRoute.self, from: encoder.encode(route))
        XCTAssertEqual(legacy.points[0].time.timeIntervalSince1970, 1_000, accuracy: 0.001)
    }
}
