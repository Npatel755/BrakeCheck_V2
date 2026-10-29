import Foundation
import XCTest
@testable import BrakeCheckCore

final class SpeedLogTests: XCTestCase {
    private func point(_ seconds: Double, _ speed: Double) -> SpeedLogPoint {
        SpeedLogPoint(time: Date(timeIntervalSince1970: seconds), metersPerSecond: speed)
    }

    private func withFile(_ content: String, extension suffix: String = "csv",
                          _ work: (URL) throws -> Void) throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent("speed.\(suffix)")
        try Data(content.utf8).write(to: url)
        try work(url)
    }

    func testNormalizationUsesLastValidDuplicateAndRejectsInvalidValues() {
        let track = SpeedTrack(name: "test", points: [point(2, 20), point(1, 10), point(1, 12),
            point(1, .nan), point(.nan, 3), point(.infinity, 3), point(3, -1), point(4, 91)])
        XCTAssertEqual(track.points.map(\.metersPerSecond), [12, 20])
        XCTAssertEqual(track.speed(at: Date(timeIntervalSince1970: 1.5)), 16)
        XCTAssertNil(track.speed(at: Date(timeIntervalSince1970: .nan)))
        XCTAssertFalse(track.covers(Date(timeIntervalSince1970: .infinity)))
    }

    func testExactSamplesSurviveLongGapsButGapIsNotCovered() {
        let track = SpeedTrack(name: "gaps", points: [point(0, 10), point(10, 20), point(20, 30), point(21, 32)])
        XCTAssertEqual(track.speed(at: Date(timeIntervalSince1970: 10)), 20)
        XCTAssertFalse(track.covers(Date(timeIntervalSince1970: 5)))
        XCTAssertEqual(track.speed(at: Date(timeIntervalSince1970: 20.5)), 31)
        XCTAssertEqual(track.speed(at: Date(timeIntervalSince1970: -2)), 10)
        XCTAssertNil(track.speed(at: Date(timeIntervalSince1970: -2.01)))
    }

    func testQuotedCSVFieldsDoNotShiftSpeedOrCoordinates() throws {
        try withFile("time,note,speed_mph\n100,\"slow, then stop\",10\n101,\"said \"\"go\"\"\",20\n") { url in
            let track = try SpeedLogReader.load(url)
            XCTAssertEqual(track.points.map(\.metersPerSecond), [4.4704, 8.9408])
        }
        try withFile("\u{FEFF}time;note;speed_kmh\n100;\"gps, ready\";36\n101;\"a;b\";72\n") { url in
            XCTAssertEqual(try SpeedLogReader.load(url).points.map(\.metersPerSecond), [10, 20])
        }
    }

    func testInvalidTimestampRowsCannotBecomeAReplayTrack() throws {
        XCTAssertNil(SpeedLogReader.parseTime("nan"))
        XCTAssertNil(SpeedLogReader.parseTime("inf"))
        try withFile("unix_time,speed_mps\nnan,1\ninf,2\n100,3\n100,4\n") { url in
            XCTAssertThrowsError(try SpeedLogReader.load(url))
        }
    }

    func testGPSDerivationRejectsLongGapsAndInvalidCoordinateBridges() {
        let fixes: [(Date, Double, Double)] = [(0, 0, 0), (1, 0, 0.0001), (2, 100, 0),
            (3, 0, 0.0003), (100, 0, 0.0004)].map { (Date(timeIntervalSince1970: $0.0), $0.1, $0.2) }
        let points = SpeedLogReader.speeds(fromFixes: fixes)
        XCTAssertEqual(points.count, 1)
        XCTAssertEqual(points.first?.time, Date(timeIntervalSince1970: 0.5))
        XCTAssertEqual(points.first!.metersPerSecond, 11.1195, accuracy: 0.001)
    }

    func testNamespacedGPXAndSeparateSegmentsDoNotInventTravelAcrossBreaks() throws {
        let xml = """
        <g:gpx xmlns:g="http://www.topografix.com/GPX/1/1"><g:trk>
          <g:trkseg>
            <g:trkpt lat="0" lon="0"><g:time>2026-10-26T12:00:00Z</g:time></g:trkpt>
            <g:trkpt lat="0" lon="0.0001"><g:time>2026-10-26T12:00:01Z</g:time></g:trkpt>
          </g:trkseg><g:trkseg>
            <g:trkpt lat="0" lon="0.001"><g:time>2026-10-26T12:00:02Z</g:time></g:trkpt>
            <g:trkpt lat="0" lon="0.0011"><g:time>2026-10-26T12:00:03Z</g:time></g:trkpt>
          </g:trkseg>
        </g:trk></g:gpx>
        """
        try withFile(xml, extension: "gpx") { url in
            let track = try SpeedLogReader.load(url)
            XCTAssertEqual(track.points.count, 2)
            XCTAssertTrue(track.points.allSatisfy { abs($0.metersPerSecond - 11.1195) < 0.001 })
            XCTAssertNil(track.speed(at: SpeedLogReader.parseTime("2026-10-26T12:00:01.500Z")!))
        }
    }

    func testInvalidGPXSpeedsFallBackToObservedPositions() throws {
        let xml = """
        <gpx><trk><trkseg>
          <trkpt lat="0" lon="0"><time>2026-10-26T12:00:00Z</time><speed>nan</speed></trkpt>
          <trkpt lat="0" lon="0.0001"><time>2026-10-26T12:00:01Z</time><speed>-1</speed></trkpt>
          <trkpt lat="0" lon="0.0002"><time>2026-10-26T12:00:02Z</time><speed>inf</speed></trkpt>
        </trkseg></trk></gpx>
        """
        try withFile(xml, extension: "gpx") { url in
            let track = try SpeedLogReader.load(url)
            XCTAssertEqual(track.points.count, 2)
            XCTAssertTrue(track.points.allSatisfy { abs($0.metersPerSecond - 11.1195) < 0.001 })
        }
    }

    func testSameSecondTripsDoNotOverwriteAndWriterRoundTrips() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let writer = SpeedLogWriter(directory: folder)
        let start = Date(timeIntervalSince1970: 1000)
        writer.start(at: start)
        let first = try XCTUnwrap(writer.url)
        writer.append(speed: 10, accuracy: 1, at: start)
        writer.append(speed: 20, accuracy: 1, at: start.addingTimeInterval(1))
        writer.stop()
        let original = try Data(contentsOf: first)
        writer.start(at: start)
        XCTAssertNotEqual(writer.url, first)
        writer.append(speed: .nan, accuracy: 1, at: start)
        writer.append(speed: 5, accuracy: .infinity, at: start)
        writer.append(speed: -1, accuracy: 1, at: start)
        writer.append(speed: 5, accuracy: 1, at: start)
        let second = try XCTUnwrap(writer.url)
        writer.stop()
        XCTAssertEqual(try Data(contentsOf: first), original)
        XCTAssertEqual(try SpeedLogReader.load(first).points.map(\.metersPerSecond), [10, 20])
        XCTAssertEqual(try String(contentsOf: second, encoding: .utf8).components(separatedBy: "\n").count, 3)
    }

    func testMalformedCSVRowsBreakInterpolationInsteadOfHidingMissingSpeed() throws {
        try withFile("time,speed_mps\n100,10\n101,nan\n102,20\n103,22\n") { url in
            let track = try SpeedLogReader.load(url)
            XCTAssertNil(track.speed(at: Date(timeIntervalSince1970: 101)))
            XCTAssertEqual(track.speed(at: Date(timeIntervalSince1970: 102.5)), 21)
        }
    }

    func testMissingGPXCoordinatesBreakExplicitSpeedContinuity() throws {
        let xml = """
        <gpx><trk><trkseg>
          <trkpt lat="0" lon="0"><time>2026-10-26T12:00:00Z</time><speed>5</speed></trkpt>
          <trkpt><time>2026-10-26T12:00:01Z</time><speed>50</speed></trkpt>
          <trkpt lat="0" lon="0"><time>2026-10-26T12:00:02Z</time><speed>6</speed></trkpt>
          <trkpt lat="0" lon="0"><time>2026-10-26T12:00:03Z</time><speed>8</speed></trkpt>
        </trkseg></trk></gpx>
        """
        try withFile(xml, extension: "gpx") { url in
            let track = try SpeedLogReader.load(url)
            XCTAssertEqual(track.points.map(\.metersPerSecond), [5, 6, 8])
            XCTAssertNil(track.speed(at: SpeedLogReader.parseTime("2026-10-26T12:00:01Z")!))
            XCTAssertEqual(track.speed(at: SpeedLogReader.parseTime("2026-10-26T12:00:02.500Z")!), 7)
        }
    }
}
