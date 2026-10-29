import BrakeCheckCore
import Foundation

func now() -> Double { Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000 }
func point(_ east: Double, _ north: Double = 0) -> NavigationCoordinate {
    let meters = 6_371_008.8 * Double.pi / 180
    return .init(latitude: north / meters, longitude: east / meters)
}
var results: [[String: Any]] = []
func benchmark(_ name: String, prepare: () -> (() -> Double)) {
    var elapsed: [Double] = [], preparation: [Double] = [], checksum = 0.0
    for _ in 0..<5 {
        let start = now(), work = prepare()
        preparation.append((now() - start) * 1000)
        let before = now(); checksum = work()
        elapsed.append((now() - before) * 1000)
    }
    results.append(["name": name, "medianMilliseconds": elapsed.sorted()[2],
        "preparationMedianMilliseconds": preparation.sorted()[2], "checksum": checksum,
        "samplesMilliseconds": elapsed])
}
let route = (0...20_000).map { point(Double($0) * 5 - 50_000) }
benchmark("gps_200_updates_20000_segments") {
    var tracker = NavigationProgressTracker(coordinates: route, routeDistance: 100_000,
        stepDistances: [50_000, 50_000])
    return {
        var checksum = 0.0
        for index in 0..<200 {
            let time = Double(index) + 10
            if let value = tracker.update(coordinate: point(Double(index) * 12, 4), horizontalAccuracy: 5,
                timestamp: time, now: time, courseDegrees: 90, courseAccuracy: 5, speed: 12) {
                checksum += value.progressMeters + value.distanceFromRoute
            }
        }
        return checksum
    }
}
let cameraRoute = (0...20_000).map { point(0, Double($0) * 5) }
let lane = LanePath(bands: (0..<25).map { index in
    let y = 0.1 + Double(index) * 0.02, half = 0.45 * (0.7 - y)
    return LaneBand(y: y, left: 0.5 - half, right: 0.5 + half)
}, confidence: 0.95, capturedAt: 10, source: .neural)
let road = RoadRegion(bands: (0..<30).map { LaneBand(y: 0.05 + Double($0) * 0.02, left: 0.05, right: 0.95) },
    confidence: 0.95, capturedAt: 10)
benchmark("ar_100_projections_20000_vertices") {
    #if OPTIMIZED
    let geometry = CameraRouteGeometry(route: cameraRoute)
    #endif
    return {
        var checksum = 0.0
        for _ in 0..<100 {
            #if OPTIMIZED
            let groups = CameraRouteProjection.project(geometry: geometry, progressFraction: 0, courseDegrees: 0,
                horizontalFOV: 60, aspect: 16.0 / 9, calibration: .init(), lane: lane, road: road, at: 10)
            #else
            let groups = CameraRouteProjection.project(route: cameraRoute, progressFraction: 0, courseDegrees: 0,
                horizontalFOV: 60, aspect: 16.0 / 9, calibration: .init(), lane: lane, road: road, at: 10)
            #endif
            checksum += groups.flatMap { $0 }.reduce(0) { $0 + $1.left + $1.right + $1.y }
        }
        return checksum
    }
}
benchmark("road_mask_100_visibility_path_scans_160x96") {
    let pixels: [UInt8] = (0..<(160 * 96)).map { i in (i % 160 > 15 && i % 160 < 145 && i / 160 > 25 && i % 53 != 0) ? 1 : 0 }
    let mask = RoadVisibilityMask(width: 160, height: 96, pixels: pixels)!
    return {
        var count = 0
        for _ in 0..<100 {
            #if OPTIMIZED
            count += mask.visibilitySpans.reduce(0) { $0 + $1.endColumn - $1.firstColumn }
            #else
            for row in 0..<96 {
                for column in 0..<160 where mask.contains(x: (Double(column) + 0.5) / 160, y: 1 - (Double(row) + 0.5) / 96) {
                    count += 1
                }
            }
            #endif
        }
        return Double(count)
    }
}
var nodes: [OfflineRoadNode] = [], edges: [OfflineRoadEdge] = []
for index in 0..<5_000 {
    let a = point(0, Double(index) * 200), b = point(1_000, Double(index) * 200)
    nodes += [.init(id: "a\(index)", coordinate: a), .init(id: "b\(index)", coordinate: b)]
    edges.append(.init(id: "road\(index)", from: "a\(index)", to: "b\(index)", coordinates: [a, b]))
}
let graph = OfflineRoadGraph(name: "Benchmark", attribution: "Synthetic", nodes: nodes, edges: edges)
benchmark("offline_100_snaps_5000_roads") {
    let router = try! OfflineNavigationRouter(graph: graph)
    return {
        var checksum = 0.0
        for index in 0..<100 {
            let snap = try! router.snap(point(500, Double(index) * 200 + 2), bearing: 90)
            checksum += snap.coordinate.latitude + snap.coordinate.longitude + snap.confidence
        }
        return checksum
    }
}
let data = try! JSONSerialization.data(withJSONObject: ["benchmarks": results], options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
