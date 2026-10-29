import Foundation

/// Inputs for an isolated renderer test. Video, GPS and perception are all
/// synthetic; none of these values are published to live driving services.
public struct MockARFixture: Codable {
    public let schemaVersion: Int
    public let width: Int
    public let height: Int
    public let videoFPS: Double
    public let duration: Double
    public let horizontalFOV: Double
    public let mountHeight: Double
    public let horizon: Double
    public let route: [NavigationCoordinate]
    public let frames: [Frame]

    public struct GPS: Codable {
        public let latitude: Double
        public let longitude: Double
        public let capturedAt: Double
        public let speed: Double
        public let course: Double
        public let horizontalAccuracy: Double
        public let courseAccuracy: Double
    }
    public struct Frame: Codable {
        public let time: Double
        public let phase: String
        public let gps: GPS
        /// [y, left, right], bottom-left normalized coordinates.
        public let lane: [[Double]]
        public let road: [[Double]]
        /// [x, y, width, height], bottom-left normalized coordinates.
        public let obstacles: [[Double]]
        public let sceneAge: Double
        public let sourceAge: Double
        public let mountVerified: Bool
        public let obstaclesFresh: Bool
        public let branchAmbiguous: Bool
        public let vehicleLatency: Double
        public let laneLatency: Double
        public let roadLatency: Double
        public let criticalHeat: Bool
        public let cameraLuma: Data?
        public let laneMissed: Bool?
        public let roadMissed: Bool?
    }

    public func validate() throws {
        func validBands(_ rows: [[Double]]) -> Bool {
            rows.allSatisfy { $0.count == 3 && $0.allSatisfy(\.isFinite)
                && (0...1).contains($0[0]) && $0[1] >= 0 && $0[2] <= 1 && $0[1] < $0[2] }
                && zip(rows, rows.dropFirst()).allSatisfy { $0[0] < $1[0] }
        }
        guard schemaVersion == 1, (320...1920).contains(width), (180...1080).contains(height),
              (1...60).contains(videoFPS), duration.isFinite, (1...300).contains(duration),
              (20...150).contains(horizontalFOV), (0.5...3).contains(mountHeight), (0.35...0.9).contains(horizon),
              route.count >= 2, route.allSatisfy(\.isValid), !frames.isEmpty, frames.count <= 10_000,
              frames.first!.time == 0, frames.last!.time < duration,
              duration - frames.last!.time <= 0.2,
              zip(frames, frames.dropFirst()).allSatisfy({ $1.time > $0.time && $1.time - $0.time <= 0.2 }),
              frames.allSatisfy({ f in
                  f.time.isFinite && (0..<duration).contains(f.time) && validBands(f.lane) && validBands(f.road)
                      && (f.cameraLuma.map { $0.count == 160 * 90 } ?? true)
                      && [f.sceneAge, f.sourceAge, f.vehicleLatency, f.laneLatency, f.roadLatency].allSatisfy { $0.isFinite && $0 >= 0 }
                      && f.obstacles.allSatisfy { $0.count == 4 && $0.allSatisfy(\.isFinite)
                          && $0[0] >= 0 && $0[1] >= 0 && $0[2] > 0 && $0[3] > 0
                          && $0[0] + $0[2] <= 1 && $0[1] + $0[3] <= 1 }
                      && NavigationCoordinate(latitude: f.gps.latitude, longitude: f.gps.longitude).isValid
                      && [f.gps.capturedAt, f.gps.speed, f.gps.course, f.gps.horizontalAccuracy, f.gps.courseAccuracy].allSatisfy(\.isFinite)
              }) else { throw MockARReplayError.invalidFixture }
    }
}

public enum MockARReplayError: Error { case invalidFixture }

public struct MockARReplayResult: Codable {
    public let time: Double
    public let phase: String
    public let confidence: String
    public let reason: String
    public let mapMatched: Bool
    public let progressMeters: Double
    public let speed: Double
    public let course: Double
    public let laneAllowed: Bool
    public let roadAllowed: Bool
    public let opacity: Double
    public let continuity: Bool
    public let trackingDiagnostic: String
    /// Each group contains [y, left, right] samples from real route projection.
    public let ribbon: [[[Double]]]
}

/// Runs the production map matcher, admission policy, confidence recovery and
/// projection against deterministic mock evidence. No confidence is forced to
/// aligned to improve a demonstration; unsupported frames return no ribbon.
public enum MockARReplay {
    public static func evaluate(_ fixture: MockARFixture, visualContinuity: Bool = false) throws -> [MockARReplayResult] {
        try fixture.validate()
        let length = zip(fixture.route, fixture.route.dropFirst()).reduce(0) { $0 + $1.0.distance(to: $1.1) }
        var matcher = NavigationProgressTracker(coordinates: fixture.route, routeDistance: length, stepDistances: [length])
        let geometry = CameraRouteGeometry(route: fixture.route)
        var snapshot: NavigationProgressSnapshot?
        var confidence = CameraARConfidenceTracker()
        var smoother = CameraRouteSmoother()
        var continuity = CameraARVisualContinuity()
        var results: [MockARReplayResult] = []
        let calibration = CameraRouteCalibration(heightMeters: fixture.mountHeight, referenceHorizon: fixture.horizon)
        for f in fixture.frames {
            let now = 100 + f.time, gps = f.gps
            if let updated = matcher.update(coordinate: .init(latitude: gps.latitude, longitude: gps.longitude),
                horizontalAccuracy: gps.horizontalAccuracy, timestamp: 100 + gps.capturedAt, now: now,
                courseDegrees: gps.course, courseAccuracy: gps.courseAccuracy, speed: gps.speed) { snapshot = updated }
            let freshGPS = (0...2).contains(f.time - gps.capturedAt) && (0...35).contains(gps.horizontalAccuracy)
            let courseReliable = gps.speed >= 2 && (0..<360).contains(gps.course)
                && (0...15).contains(gps.courseAccuracy) && gps.horizontalAccuracy <= 20
            let matched = courseReliable && snapshot.map { $0.positionReliable && !$0.mapMatchAmbiguous && $0.mapMatchConfidence >= 0.75 } == true
            let policy = ARInferencePolicy(minimumLaneInterval: 0.2, heat: f.criticalHeat ? .critical : .nominal,
                vehicleLatency: f.vehicleLatency, laneLatency: f.laneLatency, roadLatency: f.roadLatency, stationary: gps.speed < 2)
            let boxes = f.obstacles.map { CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) }
            let lane = policy.laneAllowed && !f.lane.isEmpty ? LanePath(bands: bands(f.lane), confidence: 0.95,
                capturedAt: now - f.sceneAge, source: .neural) : nil
            let road = policy.roadInterval != nil && !f.road.isEmpty ? RoadRegion(bands: bands(f.road), confidence: 0.95,
                capturedAt: now - f.sceneAge, visibility: visibility(f.road, excluding: boxes)) : nil
            let observed = CameraARScene(lane: lane, road: road, laneObservedAt: now - f.sourceAge,
                roadObservedAt: now - f.sourceAge, capturedAt: now - f.sceneAge, obstacles: boxes, obstaclesFresh: f.obstaclesFresh)
            let continuation = visualContinuity ? continuity.update(observed,
                frame: f.cameraLuma.flatMap { LaneLumaFrame(width: 160, height: 90, pixels: Array($0), capturedAt: now - f.sceneAge) },
                laneMissed: f.laneMissed == true || (!policy.laneAllowed && !f.criticalHeat),
                roadMissed: f.roadMissed == true || (policy.roadInterval == nil && !f.criticalHeat), at: now) : nil
            let scene = continuation?.scene ?? observed
            let requested = CameraARConfidencePolicy.level(scene: scene, verified: f.mountVerified, routeMatched: matched,
                navigationReliable: freshGPS, branchAmbiguous: f.branchAmbiguous, at: now)
            var groups: [[LaneBand]] = []
            if requested == .aligned, let snapshot {
                groups = CameraRouteProjection.project(geometry: geometry, progressFraction: snapshot.progressMeters / length,
                    courseDegrees: gps.course, horizontalFOV: fixture.horizontalFOV, aspect: Double(fixture.width) / Double(fixture.height),
                    calibration: calibration, lane: scene.lane, road: scene.road, excluding: boxes, at: now)
            }
            let candidate: CameraARConfidence = requested == .aligned && groups.isEmpty ? .directional : requested
            let stable = confidence.update(candidate, observedAt: now - f.sceneAge)
            let displayed = smoother.update(stable == .aligned ? groups : [], at: now, lane: scene.lane, road: scene.road)
            let reason: String
            if !freshGPS { reason = "GPS stale or inaccurate" }
            else if f.sceneAge > 0.35 { reason = "Camera scene stale" }
            else if continuation?.tracking == true && stable == .aligned { reason = "Tracking current camera through model gap" }
            else if !policy.laneAllowed { reason = f.criticalHeat ? "Critical heat: auxiliary models paused" : "Lane/vehicle inference over budget" }
            else if lane == nil { reason = "Missing lane observations" }
            else if road == nil { reason = policy.roadInterval == nil ? "Road inference over budget" : "Missing road observations" }
            else if f.sourceAge > 0.8 { reason = "Source observations expired" }
            else if !f.mountVerified { reason = "Mount unverified" }
            else if !matched { reason = "Route/course not matched" }
            else if f.branchAmbiguous { reason = "Intersection turn: direction arrow" }
            else if !f.obstaclesFresh { reason = "Obstacle observations stale" }
            else if stable == .aligned { reason = "Aligned to supported mock road" }
            else if groups.isEmpty { reason = "No supported route projection" }
            else { reason = "Confirming alignment" }
            results.append(.init(time: f.time, phase: f.phase, confidence: stable.rawValue, reason: reason,
                mapMatched: matched && freshGPS, progressMeters: snapshot?.progressMeters ?? 0, speed: gps.speed, course: gps.course,
                laneAllowed: policy.laneAllowed, roadAllowed: policy.roadInterval != nil,
                opacity: continuation?.opacity ?? 1, continuity: continuation?.tracking == true,
                trackingDiagnostic: visualContinuity ? continuity.diagnostic : "disabled",
                ribbon: displayed.map { $0.map { [$0.y, $0.left, $0.right] } }))
        }
        return results
    }

    private static func bands(_ rows: [[Double]]) -> [LaneBand] {
        rows.map { LaneBand(y: $0[0], left: $0[1], right: $0[2]) }
    }
    private static func visibility(_ rows: [[Double]], excluding boxes: [CGRect]) -> RoadVisibilityMask? {
        let width = 160, height = 90
        let path = LanePath(bands: bands(rows), confidence: 1, capturedAt: 0)
        var pixels = [UInt8](repeating: 0, count: width * height)
        for row in 0..<height {
            let y = 1 - (Double(row) + 0.5) / Double(height)
            guard let bounds = path.bounds(at: y) else { continue }
            for col in 0..<width {
                let x = (Double(col) + 0.5) / Double(width)
                if x >= bounds.left && x <= bounds.right && !boxes.contains(where: { $0.contains(CGPoint(x: x, y: y)) }) {
                    pixels[row * width + col] = 1
                }
            }
        }
        return RoadVisibilityMask(width: width, height: height, pixels: pixels)
    }
}
