import BrakeCheckCore

import SwiftUI

/// Render in the same fitted/fill camera rectangle as all detector geometry.

struct CameraNavigationOverlay: View {

    @ObservedObject var navigation: NavigationService

    @ObservedObject var presentation: CameraARPresentation

    private var scene: CameraARScene? { presentation.scene }

    let geometry: CaptureFrameGeometry

    private var calibrationEstimate: CameraCalibrationEstimate? { presentation.calibrationEstimate }

    @AppStorage("navigation.cameraAR") private var enabled = true

    @AppStorage("navigation.mountHeight") private var mountHeight = 1.4

    @AppStorage("navigation.horizonOffset") private var horizonOffset = 0.0

    @AppStorage("navigation.mountYaw") private var mountYaw = 0.0

    @AppStorage("navigation.verifiedMount") private var verifiedMount = ""

    @AppStorage("navigation.verifiedHorizon") private var verifiedHorizon = 0.0

    @AppStorage("navigation.verifiedYawEstimate") private var verifiedYawEstimate = 0.0

    @State private var smoother = CameraRouteSmoother()

    @State private var ribbon: [[LaneBand]] = []

    @State private var confidenceTracker = CameraARConfidenceTracker()

    @State private var stableConfidence: CameraARConfidence = .lost

    @State private var projectionCache = CameraRouteRenderCache()

    @State private var renderedKey: CameraRouteRenderCache.Key?

    private var calibration: CameraRouteCalibration {

        CameraRouteCalibration(heightMeters: mountHeight, horizonOffset: horizonOffset, yawDegrees: mountYaw, referenceHorizon: verifiedHorizon)

    }

    private func confidence(at date: Date) -> CameraARConfidence {

        let signature = CameraMountVerification.signature(width: geometry.width, height: geometry.height,

            fov: geometry.horizontalFieldOfView, orientation: geometry.videoOrientation.rawValue, calibration: calibration)

        let instantaneous = scene?.lane.flatMap { lane in

            geometry.horizontalFieldOfView.flatMap { CameraCalibrationGeometry.estimate(lane: lane, fov: $0, aspect: geometry.aspect) }

        }

        let mountConsistent = (instantaneous ?? calibrationEstimate).map {

            abs($0.horizon - verifiedHorizon) <= 0.05 && abs($0.yawDegrees - verifiedYawEstimate) <= 3

        } ?? true

        let fix = navigation.location

        let navigationReliable = navigation.isNavigating && !navigation.isPaused && !navigation.isCalculating

            && !navigation.isRejoiningRoute && !navigation.hasArrived && navigation.foreground && navigation.hasFreshLocation

            && fix.map { (0...2).contains(date.timeIntervalSince($0.timestamp)) && (0...35).contains($0.horizontalAccuracy) } == true

        let courseReliable = fix.map { $0.speed >= 2 && (0..<360).contains($0.course)

            && (0...15).contains($0.courseAccuracy) && $0.horizontalAccuracy <= 20 } == true

        let approachingBranch = navigation.maneuverDistance.map { $0 < 35 } == true

            && NavigationManeuver.symbol(for: navigation.instruction) != "arrow.up"

        return CameraARConfidencePolicy.level(scene: scene,

            verified: signature != "unavailable" && verifiedMount == signature && mountConsistent,

            routeMatched: courseReliable && navigation.cameraMapMatchConfidence >= 0.75 && !navigation.cameraMapMatchAmbiguous,

            navigationReliable: navigationReliable, branchAmbiguous: approachingBranch,

            at: ProcessInfo.processInfo.systemUptime)

    }

    private func projected(at date: Date) -> [[LaneBand]] {

        guard enabled, confidence(at: date) == .aligned, navigation.isNavigating, !navigation.isPaused,

              !navigation.isCalculating, !navigation.isRejoiningRoute, !navigation.hasArrived,

              navigation.foreground, navigation.hasFreshLocation,

              let fix = navigation.location, (0...2).contains(date.timeIntervalSince(fix.timestamp)),

              (0...20).contains(fix.horizontalAccuracy), fix.speed >= 2,

              (0..<360).contains(fix.course), (0...15).contains(fix.courseAccuracy),

              let progress = navigation.cameraRouteProgress,

              let fov = geometry.horizontalFieldOfView, let scene else { return [] }

        let key = CameraRouteRenderCache.Key(sceneTime: scene.capturedAt, progress: progress, course: fix.course,

            route: navigation.route.map { ObjectIdentifier($0) }, fov: fov, aspect: geometry.aspect,

            height: mountHeight, horizon: verifiedHorizon + horizonOffset, yaw: mountYaw)

        if projectionCache.key == key { return projectionCache.groups }

        let result = CameraRouteProjection.project(route: navigation.cameraRouteCoordinates,

            progressFraction: progress, courseDegrees: fix.course,

            horizontalFOV: fov, aspect: geometry.aspect,

            calibration: calibration,

            lane: scene.lane, road: scene.road, excluding: scene.obstacles, at: ProcessInfo.processInfo.systemUptime)

        projectionCache.key = key; projectionCache.groups = result

        return result

    }

    var body: some View {

        if enabled && navigation.isNavigating {

            TimelineView(.animation(minimumInterval: 1.0 / 10)) { timeline in

                // Validate every draw as well as every update; a stopped camera

                // cannot leave the last smoothed path frozen over live video.

                let current = projected(at: timeline.date)

                let proposed = confidence(at: timeline.date)

                let requested: CameraARConfidence = proposed == .aligned && current.isEmpty ? .directional : proposed

                let level: CameraARConfidence = requested == .aligned && stableConfidence != .aligned ? .directional : requested

                Canvas { context, size in

                    // A newly captured mask must never clip a ribbon smoothed
                    // from the previous route/camera frame while state catches up.
                    guard level == .aligned, !current.isEmpty, renderedKey == projectionCache.key,
                          scene?.road?.visibility != nil else { return }

                    context.clip(to: presentation.visibilityPath().applying(CGAffineTransform(scaleX: size.width, y: size.height)))

                    var support = Path()

                    let corridor = scene?.lane?.bands.compactMap { band -> LaneBand? in

                        guard let road = scene?.road?.bounds(at: band.y) else { return nil }

                        let left = max(band.left, road.left), right = min(band.right, road.right)

                        return left < right ? LaneBand(y: band.y, left: left, right: right) : nil

                    } ?? []

                    for group in [corridor] {

                        guard let first = group.first else { continue }

                        support.move(to: CGPoint(x: first.left * size.width, y: (1 - first.y) * size.height))

                        for band in group.dropFirst() {

                            support.addLine(to: CGPoint(x: band.left * size.width, y: (1 - band.y) * size.height))

                        }

                        for band in group.reversed() {

                            support.addLine(to: CGPoint(x: band.right * size.width, y: (1 - band.y) * size.height))

                        }

                        support.closeSubpath()

                    }

                    context.clip(to: support)

                    for box in scene?.obstacles ?? [] {

                        var visible = Path(CGRect(origin: .zero, size: size))

                        visible.addRect(CGRect(x: (box.minX - 0.012) * size.width, y: (1 - box.maxY - 0.012) * size.height,

                            width: (box.width + 0.024) * size.width, height: (box.height + 0.024) * size.height))

                        context.clip(to: visible, style: FillStyle(eoFill: true))

                    }

                    for group in ribbon {

                        guard let first = group.first else { continue }

                        func point(_ x: Double, _ y: Double) -> CGPoint {

                            CGPoint(x: x * size.width, y: (1 - y) * size.height)

                        }

                        var path = Path()

                        path.move(to: point(first.left, first.y))

                        for band in group.dropFirst() { path.addLine(to: point(band.left, band.y)) }

                        for band in group.reversed() { path.addLine(to: point(band.right, band.y)) }

                        path.closeSubpath()

                        context.fill(path, with: .linearGradient(Gradient(colors: [.cyan.opacity(0.65), .cyan.opacity(0.18)]),

                            startPoint: CGPoint(x: size.width / 2, y: size.height),

                            endPoint: CGPoint(x: size.width / 2, y: 0)))

                        context.stroke(path, with: .color(.cyan.opacity(0.9)), lineWidth: 1.5)

                        // Repeated forward chevrons shrink with perspective.

                        for index in stride(from: 2, to: group.count - 1, by: 8) {

                            let band = group[index]

                            let width = band.right - band.left

                            var arrow = Path()

                            arrow.move(to: point(band.left + width * 0.15, band.y))

                            arrow.addLine(to: point((band.left + band.right) / 2, band.y + width * geometry.aspect * 0.30))

                            arrow.addLine(to: point(band.right - width * 0.15, band.y))

                            context.stroke(arrow, with: .color(.white.opacity(0.85)), style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))

                        }

                    }

                }

                .onAppear {
                    ribbon = smoother.update(current, at: ProcessInfo.processInfo.systemUptime, lane: scene?.lane, road: scene?.road)
                    renderedKey = projectionCache.key
                }

                .onChange(of: timeline.date) { _, _ in

                    ribbon = smoother.update(current, at: ProcessInfo.processInfo.systemUptime, lane: scene?.lane, road: scene?.road)
                    renderedKey = projectionCache.key

                    stableConfidence = confidenceTracker.update(requested, observedAt: scene?.capturedAt ?? 0)

                    presentation.setLevel(level)
                    presentation.setGuidanceDetail(alignmentRequirement(at: timeline.date, proposed: proposed, hasProjection: !current.isEmpty))

                }

            }

            .allowsHitTesting(false)

            .accessibilityLabel("Camera navigation: \(navigation.instruction)")

        }

    }

    private func alignmentRequirement(at date: Date, proposed: CameraARConfidence, hasProjection: Bool) -> String {
        if navigation.isPaused { return navigation.status }
        if navigation.isCalculating { return "Calculating route" }
        let signature = CameraMountVerification.signature(width: geometry.width, height: geometry.height,
            fov: geometry.horizontalFieldOfView, orientation: geometry.videoOrientation.rawValue, calibration: calibration)
        if verifiedMount != signature || signature == "unavailable" {
            return presentation.mountConfirmed ? "Mount saved - Waiting for road alignment" : "Confirm the mount in Camera setup while parked"
        }
        guard navigation.hasFreshLocation, let fix = navigation.location,
              (0...2).contains(date.timeIntervalSince(fix.timestamp)), (0...35).contains(fix.horizontalAccuracy) else {
            return "Waiting for accurate GPS location"
        }
        guard let scene, ProcessInfo.processInfo.systemUptime - scene.capturedAt <= 0.35 else { return "Waiting for fresh road tracking" }
        guard let lane = scene.lane, lane.source != .markings else { return "Waiting for trained lane detection" }
        if let fov = geometry.horizontalFieldOfView,
           let current = CameraCalibrationGeometry.estimate(lane: lane, fov: fov, aspect: geometry.aspect),
           abs(current.horizon - verifiedHorizon) > 0.05 || abs(current.yawDegrees - verifiedYawEstimate) > 3 {
            return "Road alignment uncertain · re-align if the mount moved"
        }
        if scene.road?.visibility == nil { return "Waiting for road segmentation" }
        if !scene.obstaclesFresh { return "Waiting for vehicle detections" }
        if fix.speed < 2 { return "Direction only while stopped or moving slowly" }
        if fix.courseAccuracy < 0 || fix.courseAccuracy > 15 { return "Waiting for accurate travel direction" }
        if navigation.cameraMapMatchAmbiguous || navigation.cameraMapMatchConfidence < 0.75 { return "Confirming the road against your route" }
        if navigation.maneuverDistance.map({ $0 < 35 }) == true && NavigationManeuver.symbol(for: navigation.instruction) != "arrow.up" {
            return "Turn ahead · follow the direction arrow"
        }
        if proposed == .aligned && !hasProjection { return "Route and lane alignment uncertain" }
        return "Stabilizing road alignment"
    }

}

private final class CameraRouteRenderCache {

    struct Key: Equatable {

        let sceneTime: TimeInterval

        let progress: Double

        let course: Double

        let route: ObjectIdentifier?

        let fov: Double

        let aspect: Double

        let height: Double

        let horizon: Double

        let yaw: Double

    }

    var key: Key?

    var groups: [[LaneBand]] = []

}
