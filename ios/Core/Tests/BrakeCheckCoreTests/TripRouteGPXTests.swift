import XCTest
@testable import BrakeCheckCore

final class TripRouteGPXTests: XCTestCase {
    private func point(_ lat: Double, _ lon: Double, _ seconds: TimeInterval) -> TripRoute.Point {
        TripRoute.Point(latitude: lat, longitude: lon, time: Date(timeIntervalSince1970: 1_700_000_000 + seconds))
    }

    func testEmptyOrSinglePointRouteHasNoDocument() {
        XCTAssertNil(TripRoute().gpxDocument(name: "Trip"))
        XCTAssertNil(TripRoute(points: [point(33.7, -84.4, 0)]).gpxDocument(name: "Trip"))
    }

    func testTrackSegmentsFollowGPSGapsAndEventsBecomeWaypoints() throws {
        let route = TripRoute(destination: "Home", points: [
            point(33.7000, -84.4000, 0), point(33.7001, -84.4001, 5),
            point(33.7010, -84.4010, 60), point(33.7011, -84.4011, 65),
        ], events: [
            TripRoute.Event(kind: .brakeAlert, time: Date(timeIntervalSince1970: 1_700_000_004), latitude: 33.7001,
                            longitude: -84.4001, speedMPH: 42, closingSeconds: 1.4),
            TripRoute.Event(kind: .hardBrake, time: Date(timeIntervalSince1970: 1_700_000_062), latitude: nil,
                            longitude: nil, speedMPH: nil, closingSeconds: nil),
        ])
        let gpx = try XCTUnwrap(route.gpxDocument(name: "Trip <1> & \"two\""))
        XCTAssertTrue(gpx.hasPrefix("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<gpx version=\"1.1\" creator=\"BrakeSense\""))
        XCTAssertEqual(gpx.components(separatedBy: "<trkseg>").count - 1, 2, "a 55 s gap splits the track")
        XCTAssertEqual(gpx.components(separatedBy: "<trkpt ").count - 1, 4)
        XCTAssertEqual(gpx.components(separatedBy: "<wpt ").count - 1, 1, "events without a location are skipped")
        XCTAssertTrue(gpx.contains("<name>BRAKE alert</name><desc>BRAKE alert · 42 mph · 1.4 s to impact</desc>"))
        XCTAssertTrue(gpx.contains("<name>Trip &lt;1&gt; &amp; &quot;two&quot;</name>"))
        XCTAssertTrue(gpx.contains("lat=\"33.700000\" lon=\"-84.400000\""))
        XCTAssertTrue(gpx.contains("<time>2023-11-14T22:13:20Z</time>"))
        XCTAssertTrue(gpx.hasSuffix("</trk>\n</gpx>\n"))
    }
}
