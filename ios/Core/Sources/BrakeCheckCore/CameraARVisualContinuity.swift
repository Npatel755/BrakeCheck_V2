import Foundation

/// Successive-frame AR tracking with bidirectional image checks. Image
/// keyframes can advance; original model timestamps cannot. This supports
/// missed inference, never a frozen camera or an observed negative result.
public struct CameraARVisualContinuity {
    public struct Output {
        public let scene: CameraARScene
        public let opacity: Double
        public let tracking: Bool
    }
    private var keyframe: LaneModelMeasurement?
    private var referenceFrame: LaneModelMeasurement?
    private var laneOrigin: (time: TimeInterval, confidence: Double)?
    private var roadOrigin: (road: RoadRegion, lane: LanePath, time: TimeInterval)?
    private var lastTime: TimeInterval?
    private var cycleError = 0.0
    private var matches = 0
    public private(set) var diagnostic = "measured"
    public init() {}
    public mutating func reset() {
        keyframe = nil; referenceFrame = nil; laneOrigin = nil; roadOrigin = nil; lastTime = nil; cycleError = 0; matches = 0
    }

    public mutating func update(_ scene: CameraARScene, frame: LaneLumaFrame?,
                                laneMissed: Bool, roadMissed: Bool, at time: TimeInterval) -> Output {
        let unchanged = Output(scene: scene, opacity: 1, tracking: false)
        guard time.isFinite, time >= scene.capturedAt, time - scene.capturedAt <= 0.1,
              lastTime.map({ time > $0 && time - $0 <= 0.35 }) ?? true else { reset(); return unchanged }
        lastTime = time
        if scene.lane == nil && !laneMissed { keyframe = nil; referenceFrame = nil; laneOrigin = nil; matches = 0; cycleError = 0 }
        if scene.road == nil && !roadMissed { roadOrigin = nil }
        let freshLane = scene.lane.flatMap { lane -> LanePath? in
            !scene.isPredicted && lane.source == .neural && lane.isUsable(at: time)
                && abs(scene.laneObservedAt - lane.capturedAt) < 0.001 ? lane : nil
        }
        let freshRoad = scene.road.flatMap { road -> RoadRegion? in
            !scene.isPredicted && road.isUsable(at: time)
                && abs(scene.roadObservedAt - road.capturedAt) < 0.001 ? road : nil
        }
        let needsTracking = (scene.lane == nil && laneMissed) || (scene.road == nil && roadMissed)
        var verification: LaneVisualTracker.VerifiedProjection?
        var laneContradiction = false
        if needsTracking, scene.obstaclesFresh, let keyframe, let frame,
           abs(frame.capturedAt - time) < 0.001,
           let result = LaneVisualTracker.cycleCheckedProject(keyframe.lane, from: keyframe.frame, to: frame, excluding: scene.obstacles) {
            let differences = freshLane.map { measured in result.lane.bands.compactMap { band -> Double? in
                measured.bounds(at: band.y).map { max(abs($0.left - band.left), abs($0.right - band.right)) }
            } } ?? []
            if freshLane == nil || (differences.count >= 8 && (differences.max() ?? 1) <= 0.025) {
                verification = result
            } else { laneContradiction = true }
        }
        if verification == nil, needsTracking, freshLane == nil, scene.obstaclesFresh,
           let referenceFrame, let frame, abs(frame.capturedAt - time) < 0.001 {
            verification = LaneVisualTracker.cycleCheckedProject(referenceFrame.lane, from: referenceFrame.frame,
                to: frame, excluding: scene.obstacles, maximumSourceAge: 3)
        }
        if let freshLane, let frame, abs(frame.capturedAt - time) < 0.001,
           let measurement = LaneModelMeasurement(lane: freshLane, frame: frame) {
            keyframe = measurement; referenceFrame = measurement; laneOrigin = (scene.laneObservedAt, freshLane.confidence)
        }
        if let freshLane, let freshRoad { roadOrigin = (freshRoad, freshLane, scene.roadObservedAt) }
        guard needsTracking else { cycleError = 0; matches = 0; diagnostic = "measured"; return unchanged }
        // A fresh measured lane independently supplies road-mask motion when
        // segmentation misses. Do not require a second, weaker image match to
        // override that current neural geometry. Both-model gaps still require
        // the bidirectional camera match above.
        guard scene.obstaclesFresh, let frame,
              verification != nil || (freshLane != nil && !laneContradiction && scene.road == nil && roadMissed) else {
            diagnostic = "image match or measured-lane agreement unavailable"; return unchanged
        }
        cycleError += verification?.cycleError ?? 0
        guard cycleError <= 0.025 else { diagnostic = "cycle error budget exceeded: \(cycleError)"; keyframe = nil; return unchanged }
        matches += 1
        var lane = scene.lane, road = scene.road
        var laneTime = scene.laneObservedAt, roadTime = scene.roadObservedAt
        var maximumAge = 0.0
        if lane == nil, laneMissed, let origin = laneOrigin, let verification, (0...3).contains(time - origin.time) {
            maximumAge = time - origin.time
            let confidence = origin.confidence - 0.025 * maximumAge - cycleError * 2
            lane = LanePath(bands: verification.lane.bands, confidence: confidence, capturedAt: time, source: .neural)
            laneTime = origin.time
        }
        if road == nil, roadMissed, let origin = roadOrigin, let lane,
           (0...3).contains(time - origin.time),
           let projected = RoadVisualProjection.project(origin.road, from: origin.lane, to: lane, at: time, maximumSourceAge: 3) {
            maximumAge = max(maximumAge, time - origin.time)
            road = RoadRegion(bands: projected.bands, confidence: origin.road.confidence - 0.025 * (time - origin.time) - cycleError * 2,
                capturedAt: time, visibility: projected.visibility)
            roadTime = origin.time
        }
        guard let lane, let road, lane.isUsable(at: time), road.isUsable(at: time),
              lane.confidence >= 0.75, road.confidence >= 0.75, road.visibility != nil else { diagnostic = "tracked geometry/age/quality unsupported"; return unchanged }
        if let measurement = LaneModelMeasurement(lane: lane, frame: frame) { keyframe = measurement }
        let evidence = CameraARTrackingEvidence(validatedAt: time, consecutiveMatches: matches, accumulatedCycleError: cycleError)
        let result = CameraARScene(lane: lane, road: road, laneObservedAt: laneTime, roadObservedAt: roadTime,
            capturedAt: scene.capturedAt, obstacles: scene.obstacles, obstaclesFresh: scene.obstaclesFresh,
            isPredicted: true, diagnosticRoadMask: scene.diagnosticRoadMask, trackingEvidence: evidence)
        diagnostic = "tracked \(matches) captures, cycle error \(cycleError)"
        return Output(scene: result, opacity: max(0.65, 1 - maximumAge * 0.10 - cycleError * 2), tracking: true)
    }
}
