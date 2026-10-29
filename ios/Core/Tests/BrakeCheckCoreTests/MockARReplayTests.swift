import XCTest
@testable import BrakeCheckCore

final class MockARReplayTests: XCTestCase {
    private func fixture(change: (Int, inout [String: Any]) -> Void = { _, _ in }) throws -> MockARFixture {
        let rows = (0..<25).map { index -> [Double] in
            let y = 0.08 + Double(index) * 0.02, half = 0.45 * (0.7 - y)
            return [y, 0.5 - half, 0.5 + half]
        }
        var frames: [[String: Any]] = []
        for index in 0..<20 {
            let t = Double(index) / 10
            var f: [String: Any] = ["time": t, "phase": "unit fixture",
                "gps": ["latitude": 40 + 9 * t / 111_320, "longitude": -74, "capturedAt": t,
                        "speed": 9, "course": 0, "horizontalAccuracy": 3, "courseAccuracy": 3],
                "lane": rows, "road": rows.map { [$0[0], max(0.01, $0[1] - 0.15), min(0.99, $0[2] + 0.15)] },
                "obstacles": [[Double]](), "sceneAge": 0, "sourceAge": 0,
                "mountVerified": true, "obstaclesFresh": true, "branchAmbiguous": false,
                "vehicleLatency": 0.04, "laneLatency": 0.05, "roadLatency": 0.04, "criticalHeat": false]
            change(index, &f); frames.append(f)
        }
        let document: [String: Any] = ["schemaVersion": 1, "width": 960, "height": 540,
            "videoFPS": 30, "duration": 2, "horizontalFOV": 60, "mountHeight": 1.4, "horizon": 0.7,
            "route": [["latitude": 40, "longitude": -74], ["latitude": 40.003, "longitude": -74]], "frames": frames]
        return try JSONDecoder().decode(MockARFixture.self, from: JSONSerialization.data(withJSONObject: document))
    }

    func testHealthyMockInputsUseRealMapMatchAndConfirmBeforeRibbonAppears() throws {
        let result = try MockARReplay.evaluate(fixture())
        XCTAssertEqual(result[0].confidence, "directional")
        XCTAssertTrue(result[0].ribbon.isEmpty)
        XCTAssertEqual(result[5].confidence, "aligned")
        XCTAssertTrue(result[5].mapMatched)
        XCTAssertFalse(result[5].ribbon.isEmpty)
        XCTAssertGreaterThan(result.last!.progressMeters, result.first!.progressMeters)
    }

    func testStaleGPSAndCameraSceneCannotKeepAlignedGraphics() throws {
        for field in ["gps", "sceneAge"] {
            let input = try fixture { i, f in
                guard i >= 7 else { return }
                if field == "gps" {
                    var gps = f["gps"] as! [String: Any]; gps["capturedAt"] = -3; f["gps"] = gps
                } else { f["sceneAge"] = 0.5 }
            }
            let result = try MockARReplay.evaluate(input)
            XCTAssertEqual(result[6].confidence, "aligned")
            XCTAssertTrue(result.dropFirst(7).allSatisfy { $0.confidence == "lost" && $0.ribbon.isEmpty })
        }
    }

    func testTurnAndMissingPerceptionDowngradeImmediatelyThenReconfirm() throws {
        for field in ["branchAmbiguous", "lane", "road", "obstaclesFresh", "mountVerified"] {
            let input = try fixture { i, f in
                guard (7..<12).contains(i) else { return }
                switch field {
                case "lane", "road": f[field] = [[Double]]()
                case "obstaclesFresh", "mountVerified": f[field] = false
                default: f[field] = true
                }
            }
            let result = try MockARReplay.evaluate(input)
            XCTAssertTrue(result[7..<12].allSatisfy { $0.confidence == "directional" && $0.ribbon.isEmpty }, field)
            XCTAssertEqual(result[12].confidence, "directional", field)
            XCTAssertEqual(result[17].confidence, "aligned", field)
        }
    }

    func testOverloadUsesActualAdmissionPolicyAndCannotLeaveHeldRibbon() throws {
        let result = try MockARReplay.evaluate(fixture { i, f in
            if i >= 7 { f["vehicleLatency"] = 0.28; f["laneLatency"] = 0.34 }
        })
        XCTAssertTrue(result.dropFirst(7).allSatisfy {
            !$0.laneAllowed && !$0.roadAllowed && $0.confidence == "lost" && $0.ribbon.isEmpty
        })
    }

    func testObstacleHolesSplitProjectedRibbon() throws {
        let result = try MockARReplay.evaluate(fixture { _, f in
            f["obstacles"] = [[0.35, 0.29, 0.30, 0.12]]
        })
        let groups = result[5].ribbon
        XCTAssertEqual(result[5].confidence, "aligned")
        XCTAssertGreaterThanOrEqual(groups.count, 2)
        XCTAssertTrue(groups.flatMap { $0 }.allSatisfy { $0[0] < 0.28 || $0[0] > 0.42 })
    }

    func testCorruptAndOutOfOrderFixtureCannotReachProjection() throws {
        let invalidRows = try fixture { i, f in if i == 5 { f["lane"] = [[0.3,0.2]] } }
        XCTAssertThrowsError(try MockARReplay.evaluate(invalidRows))
        let duplicate = try fixture { i, f in if i == 5 { f["time"] = 0.4 } }
        XCTAssertThrowsError(try MockARReplay.evaluate(duplicate))
    }
}
