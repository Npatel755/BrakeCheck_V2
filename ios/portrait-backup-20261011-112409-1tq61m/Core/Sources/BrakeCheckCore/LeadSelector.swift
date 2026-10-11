import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// A fallback mounting guide, never a claim that road markings were detected.
/// Coordinates use Vision's normalized bottom-left origin.
public struct LeadCorridor {
    public let centerX = 0.5
    public let bottomY = 0.02
    public let horizonY = 0.62
    public let nearHalfWidth = 0.29
    public let farHalfWidth = 0.045
    public init() {}
    public func halfWidth(at y: Double) -> Double? {
        guard y.isFinite, (bottomY...horizonY).contains(y) else { return nil }
        return farHalfWidth + (nearHalfWidth - farHalfWidth) * (horizonY - y) / (horizonY - bottomY)
    }
    public func contains(_ box: CGRect) -> Bool {
        guard LeadVehicle.validBox(box), let width = halfWidth(at: Double(box.minY)) else { return false }
        return abs(Double(box.midX) - centerX) <= width
    }
}

/// Image evidence from the trained CFV eight-view classifier. These scores
/// describe visible vehicle orientation, not a measured travel velocity.
public struct VehicleViewpoint {
    public let front: Double
    public let rear: Double
    public let side: Double
    public let capturedAt: TimeInterval
    /// Counts independent classifier captures, never repeated detector frames.
    public let rearEvidenceCount: Int
    public let rearEvidenceSpan: TimeInterval
    public init?(scores: [Double], capturedAt: TimeInterval) {
        guard scores.count == 8, capturedAt.isFinite,
              scores.allSatisfy({ $0.isFinite && (0...1).contains($0) }),
              abs(scores.reduce(0, +) - 1) <= 0.05 else { return nil }
        front = scores[0] + scores[1] + scores[7]
        rear = scores[3] + scores[4] + scores[5]
        side = scores[2] + scores[6]
        self.capturedAt = capturedAt
        rearEvidenceCount = 1; rearEvidenceSpan = 0
    }
    init(latest: VehicleViewpoint, rearCount: Int, rearSpan: TimeInterval) {
        front = latest.front; rear = latest.rear; side = latest.side
        capturedAt = latest.capturedAt
        rearEvidenceCount = rearCount; rearEvidenceSpan = rearSpan
    }
    public func isFresh(at time: TimeInterval) -> Bool {
        time.isFinite && time >= capturedAt && time - capturedAt <= 0.90
    }
    public func isFrontFacing(at time: TimeInterval) -> Bool {
        isFresh(at: time) && front >= 0.68 && front - rear >= 0.30
    }
    public func isRearFacing(at time: TimeInterval) -> Bool {
        isFresh(at: time) && rear >= 0.60 && rear - front >= 0.20
    }
    public func hasConfirmedRearView(at time: TimeInterval) -> Bool {
        isRearFacing(at: time) && rearEvidenceCount >= 2 && rearEvidenceSpan >= 0.15 - 1e-6
    }
}

public struct LeadVehicle {
    public let box: CGRect
    public let label: String
    public let confidence: Double
    public let viewpoint: VehicleViewpoint?
    public let trackID: UUID?
    public let travel: VehicleTravelEvidence?
    public init(box: CGRect, label: String, confidence: Double, viewpoint: VehicleViewpoint? = nil,
                trackID: UUID? = nil, travel: VehicleTravelEvidence? = nil) {
        self.box = box; self.label = label.lowercased(); self.confidence = confidence
        self.viewpoint = viewpoint
        self.trackID = trackID
        self.travel = travel
    }
    public static func validBox(_ box: CGRect) -> Bool {
        [box.minX, box.minY, box.width, box.height].allSatisfy { $0.isFinite }
            && box.width >= 0.015 && box.height >= 0.015
            && box.minX >= 0 && box.maxX <= 1 && box.minY >= 0 && box.maxY <= 1
    }
    public var assumedWidthMeters: Double {
        switch label {
        case "motorcycle", "motorbike": return 0.8
        case "car": return 1.8
        default: return 2.5
        }
    }
}

public struct LeadSelection {
    public let id: UUID
    public let vehicle: LeadVehicle
    /// False in a detection gap: draw the track, never derive speed/TTC.
    public let isFreshDetection: Bool
    /// Leads require lane support or road membership with forward evidence.
    /// Collision hazards use separate lane, road, or camera-trajectory checks.
    public let isPathSupported: Bool
    /// Hazard authorization is separate from same-direction lead selection.
    public var isCollisionHazard = false
    /// Camera trajectory supports a possible obstacle, without a measured lane.
    public var isCameraProjectedPath = false
    /// The same vehicle ID passed rear confirmation and remains associated.
    public var hasConfirmedRearIdentity = false
    /// Current classification is weak/missing; identity retains earlier evidence.
    public var isRearConfirmationRetained = false
}

/// Soft lane membership, time-based confirmation and two confidence thresholds.
/// A front-facing match clears a target immediately, including visual coasting.
public struct LeadSelector {
    public let corridor = LeadCorridor()
    private let confirmationSeconds = 0.20
    private let minimumObservations = 3
    private let retentionSeconds = 0.90
    private let visualRetentionSeconds = 3.0
    private let laneRetentionSeconds = 1.80
    private let rearConfirmationGraceSeconds = 6.0
    private var lastUpdate: TimeInterval?
    private var track: Track?
    private var challenger: Track?
    private let requiresRearView: Bool
    private var pathPrior: LanePath?
    private var imageAspect = 16.0 / 9.0

    private struct Track {
        let id: UUID
        let started: TimeInterval
        var lastDetection: TimeInterval
        var vehicle: LeadVehicle
        var observations = 1
        var confirmed = false
        var velocityX = 0.0
        var velocityY = 0.0
        var lastRearVerifiedAt: TimeInterval? = nil
        var lastPathVerifiedAt: TimeInterval? = nil
        var sideCaptures: [TimeInterval] = []
        var outsidePathSince: TimeInterval? = nil
        var outsidePathObservations = 0
    }
    private struct Match {
        var vehicle: LeadVehicle?
        var ambiguous = false
    }

    public init(requiresRearView: Bool = false) { self.requiresRearView = requiresRearView }
    public mutating func reset() { track = nil; challenger = nil; lastUpdate = nil; pathPrior = nil }

    public mutating func update(_ detections: [LeadVehicle], at time: TimeInterval,
                                lane: LanePath? = nil, trackedBox: CGRect? = nil,
                                road: RoadRegion? = nil,
                                imageAspect: Double = 16.0 / 9.0) -> LeadSelection? {
        guard time.isFinite, imageAspect.isFinite, (0.1...10).contains(imageAspect) else {
            reset(); return nil
        }
        // Vision boxes are normalized independently along each image axis.
        // Their physical shape depends on the actual camera/replay dimensions.
        if abs(self.imageAspect - imageAspect) > 0.001 { reset() }
        self.imageAspect = imageAspect
        if let previous = lastUpdate {
            guard time > previous else { reset(); return nil }
            if time - previous > 0.75 {
                // A brief inference stall is not an object disappearance.
                // Resume only with the same detected ID or a plausible visual
                // track; a different nearby car must still acquire on its own.
                let continuous = track.map { current in
                    guard current.confirmed, time - current.lastDetection <= visualRetentionSeconds else { return false }
                    let visual = trackedBox.map { plausibleTrackedBox($0, previous: current.vehicle.box) } == true
                    let detected = detections.contains { vehicle in
                        vehicle.trackID != nil && vehicle.trackID == current.vehicle.trackID
                            && plausibleTrackedBox(vehicle.box, previous: current.vehicle.box)
                    }
                    return visual || detected || canCoastInLane(current, lane: lane,
                        at: time, detections: detections, trackedBox: trackedBox)
                } ?? false
                if !continuous { track = nil }
                challenger = nil; pathPrior = nil
            }
        }
        lastUpdate = time
        let freshLane = lane.flatMap { $0.isUsable(at: time) ? $0 : nil }
        let freshRoad = road.flatMap { $0.isUsable(at: time) ? $0 : nil }
        if let freshLane { pathPrior = freshLane }
        if let prior = pathPrior, time - prior.capturedAt > 0.80 { pathPrior = nil }
        // Do this before filtering: otherwise a now-front-facing target could
        // survive as a "missing detection" on its auxiliary visual track.
        if var current = track {
            let expected = prediction(current, at: time)
            let blocked = detections.filter { ($0.viewpoint?.isFrontFacing(at: time) == true
                || $0.travel?.vetoesLead(at: time) == true)
                && Self.overlap($0.box, expected) >= 0.35 }
            // Negative orientation evidence vetoes this position even if an
            // ambiguous crossing has assigned the observed car a new ID.
            let blockedMatch = association(current, expected: expected, candidates: blocked, requiresSameID: false)
            if blockedMatch.vehicle != nil || blockedMatch.ambiguous {
                track = nil; challenger = nil
            } else if let matched = association(current, expected: expected, candidates: detections).vehicle {
                if let view = matched.viewpoint, view.isFresh(at: time),
                   view.side >= 0.70, view.side - view.rear >= 0.30 {
                    if current.sideCaptures.last.map({ view.capturedAt > $0 }) ?? true {
                        current.sideCaptures.removeAll { view.capturedAt - $0 > 1.60 }
                        current.sideCaptures.append(view.capturedAt)
                    }
                } else if let view = matched.viewpoint, view.isFresh(at: time), view.side < 0.70 {
                    current.sideCaptures = []
                }
                if current.sideCaptures.count >= 2,
                   let first = current.sideCaptures.first, let last = current.sideCaptures.last,
                   last - first >= 0.15 - 1e-6 {
                    track = nil; challenger = nil
                } else { track = current }
            }
        }
        let visible = detections.filter {
            eligible($0, minimumConfidence: 0.28) && $0.viewpoint?.isFrontFacing(at: time) != true
                && $0.travel?.vetoesLead(at: time) != true
        }
        let retained = visible.filter { vehicle in
            !requiresRearView || vehicle.viewpoint?.hasConfirmedRearView(at: time) == true
                || track.map { existing in canRetainRearIdentity(vehicle, for: existing, at: time) } == true
        }
        let acquisition = visible.filter {
            $0.confidence >= 0.45 && canAcquire($0, lane: freshLane)
                && !clearlyOutside($0.box, lane: freshLane)
                && (!requiresRearView || $0.viewpoint?.hasConfirmedRearView(at: time) == true)
        }.sorted { priority($0, lane: freshLane, road: freshRoad) > priority($1, lane: freshLane, road: freshRoad) }

        let hasVisualSupport = track.flatMap { existing in
            trackedBox.map { plausibleTrackedBox($0, previous: existing.vehicle.box) }
        } == true
        let hasSameDetection = track.map { current in
            retained.contains { $0.trackID != nil && $0.trackID == current.vehicle.trackID
                && plausibleTrackedBox($0.box, previous: current.vehicle.box) }
        } ?? false
        let laneGrace = track.map { canCoastInLane($0, lane: freshLane, at: time,
            detections: detections, trackedBox: trackedBox) } == true
        let allowedGap = hasVisualSupport || hasSameDetection ? visualRetentionSeconds
            : laneGrace ? laneRetentionSeconds : retentionSeconds
        if var current = track, time - current.lastDetection <= allowedGap {
            let predicted = prediction(current, at: time)
            let expected = trackedBox.flatMap {
                plausibleTrackedBox($0, previous: current.vehicle.box) ? $0 : nil
            } ?? predicted
            let match = association(current, expected: expected, candidates: retained)
            if match.ambiguous { track = nil; challenger = nil; return nil }
            if let seen = match.vehicle, clearlyOutside(seen.box, lane: freshLane) {
                updateTrack(&current, with: seen, at: time, lane: freshLane, road: freshRoad)
                if current.outsidePathSince == nil { current.outsidePathSince = time }
                current.outsidePathObservations += 1
                challenger = nil
                if current.outsidePathObservations >= 3,
                   time - (current.outsidePathSince ?? time) >= 0.30 - 1e-6 {
                    track = acquisition.first.map { makeTrack($0, at: time) }
                    return nil
                }
                track = current
                // Pause derived metrics immediately; only the identity gets
                // a brief lane-boundary grace period to absorb segmentation jitter.
                return current.confirmed ? selection(current, fresh: true, lane: freshLane, road: freshRoad, at: time) : nil
            }
            if match.vehicle == nil, let replacement = acquisition.first,
               abs(replacement.box.midX - current.vehicle.box.midX) < 0.025,
               abs(replacement.box.minY - current.vehicle.box.minY) < 0.04 {
                track = makeTrack(replacement, at: time); challenger = nil
                return nil
            }
            if let matched = match.vehicle, !clearlyOutside(matched.box, lane: freshLane) {
                let prior = current
                updateTrack(&current, with: matched, at: time, lane: freshLane, road: freshRoad)
                current.outsidePathSince = nil; current.outsidePathObservations = 0
                if let nearer = acquisition.first,
                   Self.overlap(nearer.box, matched.box) < 0.35,
                   (nearer.box.minY + 0.045 < matched.box.minY
                    || (requiresRearView && freshLane.map { $0.confidence >= 0.70 } == true
                        && !hasPathSupport(matched.box, lane: freshLane)
                        && hasPathSupport(nearer.box, lane: freshLane))),
                   priority(nearer, lane: freshLane, road: freshRoad) > priority(matched, lane: freshLane, road: freshRoad) + 0.04 {
                    updateChallenger(nearer, at: time, lane: freshLane, road: freshRoad)
                    if let replacement = challenger, replacement.confirmed {
                        track = replacement; challenger = nil
                        return selection(replacement, fresh: true, lane: freshLane, road: freshRoad, at: time)
                    }
                    track = current
                    // A closer possible cut-in is being confirmed: pause derived metrics.
                    return current.confirmed ? selection(current, fresh: false, lane: freshLane, road: freshRoad, at: time) : nil
                }
                challenger = nil
                track = current
                let sizeJump = abs(Double(matched.box.width / prior.vehicle.box.width) - 1) > 0.35
                    || abs(Double(matched.box.height / prior.vehicle.box.height) - 1) > 0.35
                // One noisy box cannot erase identity. Its numbers are paused
                // until a consistent detector measurement follows.
                return current.confirmed ? selection(current, fresh: !sizeJump, lane: freshLane, road: freshRoad, at: time) : nil
            }

            let box = expected
            if current.confirmed, LeadVehicle.validBox(box) {
                if clearlyOutside(box, lane: freshLane) {
                    if current.outsidePathSince == nil { current.outsidePathSince = time }
                    current.outsidePathObservations += 1
                    if current.outsidePathObservations >= 3,
                       time - (current.outsidePathSince ?? time) >= 0.30 - 1e-6 {
                        track = nil; challenger = nil; return nil
                    }
                } else {
                    current.outsidePathSince = nil; current.outsidePathObservations = 0
                }
                let visual = LeadVehicle(box: box, label: current.vehicle.label,
                    confidence: current.vehicle.confidence, viewpoint: current.vehicle.viewpoint,
                    trackID: current.vehicle.trackID, travel: current.vehicle.travel)
                guard !requiresRearView || rearIdentityIsValid(current, at: time) else {
                    track = nil; challenger = nil; return nil
                }
                if hasVisualSupport { current.vehicle = visual }
                track = current
                challenger = nil
                return LeadSelection(id: current.id, vehicle: visual, isFreshDetection: false,
                    isPathSupported: pathSupported(visual, lane: freshLane, road: freshRoad, at: time),
                    hasConfirmedRearIdentity: requiresRearView && rearIdentityIsValid(current, at: time),
                    isRearConfirmationRetained: requiresRearView)
            }
        }

        track = nil; challenger = nil
        guard let best = acquisition.first else { return nil }
        if acquisition.count > 1,
           abs(priority(best, lane: freshLane, road: freshRoad) - priority(acquisition[1], lane: freshLane, road: freshRoad)) < 0.055,
           Self.overlap(best.box, acquisition[1].box) < 0.6 { return nil }
        track = makeTrack(best, at: time)
        return nil
    }

    private func eligible(_ v: LeadVehicle, minimumConfidence: Double) -> Bool {
        guard v.confidence.isFinite, (minimumConfidence...1).contains(v.confidence),
              ["car", "truck", "bus", "motorcycle", "motorbike"].contains(v.label),
              LeadVehicle.validBox(v.box), Double(v.box.minY) < (requiresRearView ? 0.75 : corridor.horizonY) else { return false }
        let aspect = Double(v.box.width / v.box.height) * imageAspect
        return (0.25...(v.label == "car" ? 3.5 : 4.5)).contains(aspect)
    }

    private func support(_ box: CGRect, lane: LanePath?) -> (offset: Double, affinity: Double, overlap: Double) {
        let y = Double(box.minY)
        let bounds = lane?.bounds(at: y)
        let center = bounds.map { ($0.left + $0.right) / 2 } ?? corridor.centerX
        let halfWidth = bounds.map { ($0.right - $0.left) / 2 }
            ?? corridor.halfWidth(at: y) ?? 0.05
        let tolerance = bounds == nil ? 0.015 : 0.012
        let offset = abs(Double(box.midX) - center) / max(0.025, halfWidth + tolerance)
        let overlap = bounds.map {
            max(0, min(Double(box.maxX), $0.right) - max(Double(box.minX), $0.left)) / Double(box.width)
        } ?? 0
        return (offset, max(exp(-0.85 * offset * offset), 0.90 * overlap), overlap)
    }
    private func canAcquire(_ vehicle: LeadVehicle, lane: LanePath?) -> Bool {
        if !requiresRearView { return support(vehicle.box, lane: lane).offset <= 1.18 }
        // Rear evidence is obtained independently over the whole visible car.
        // Lane position ranks candidates and authorizes metrics, never crops
        // the candidate search to the estimated lane or image center.
        return true
    }
    private func clearlyOutside(_ box: CGRect, lane: LanePath?) -> Bool {
        let position = support(box, lane: lane)
        if !requiresRearView { return position.offset > 1.40 }
        if !LeadVehicle.validBox(box) { return true }
        guard let lane, lane.confidence >= 0.70, lane.bounds(at: Double(box.minY)) != nil else { return false }
        return position.overlap < 0.10 && position.offset > 1.20
    }
    private func hasPathSupport(_ box: CGRect, lane: LanePath?) -> Bool {
        guard let lane, lane.bounds(at: Double(box.minY)) != nil else { return !requiresRearView }
        return support(box, lane: lane).overlap >= 0.35
    }
    private func pathSupported(_ vehicle: LeadVehicle, lane: LanePath?, road: RoadRegion?, at time: TimeInterval) -> Bool {
        if let lane, lane.bounds(at: Double(vehicle.box.minY)) != nil {
            return hasPathSupport(vehicle.box, lane: lane)
        }
        if !requiresRearView { return true }
        // Pavement alone must never turn an oncoming/parked car into a lead.
        // Unmarked-road support requires independent, fresh forward evidence.
        return vehicle.travel?.supportsForwardTravel(at: time) == true
            && (road?.overlap(with: vehicle.box, at: time) ?? 0) >= 0.50
    }
    private func priority(_ v: LeadVehicle, lane: LanePath?, road: RoadRegion? = nil) -> Double {
        let guidance = lane ?? (requiresRearView ? pathPrior : nil)
        let position = support(v.box, lane: guidance)
        let depth = max(0, (corridor.horizonY - Double(v.box.minY)) / corridor.horizonY)
        let laneWeight = guidance.map { min(1, max(0.25, ($0.confidence - 0.35) / 0.60)) } ?? 0.25
        let positionWeight = requiresRearView ? 0.62 * laneWeight : 0.62
        let roadAffinity = road.flatMap { $0.bounds(at: Double(v.box.minY)) }.map {
            max(0, min(Double(v.box.maxX), $0.right) - max(Double(v.box.minX), $0.left)) / Double(v.box.width)
        } ?? 0
        return positionWeight * position.affinity + (0.88 - positionWeight) * depth + 0.12 * v.confidence
            + (lane == nil ? 0.12 * roadAffinity : 0)
    }
    private func makeTrack(_ v: LeadVehicle, at time: TimeInterval) -> Track {
        var result = Track(id: v.trackID ?? UUID(), started: time, lastDetection: time, vehicle: v)
        if v.viewpoint?.hasConfirmedRearView(at: time) == true {
            result.lastRearVerifiedAt = v.viewpoint?.capturedAt
        }
        return result
    }
    private func selection(_ t: Track, fresh: Bool, lane: LanePath?, road: RoadRegion?, at time: TimeInterval) -> LeadSelection {
        LeadSelection(id: t.id, vehicle: t.vehicle, isFreshDetection: fresh,
            isPathSupported: pathSupported(t.vehicle, lane: lane, road: road, at: time),
            hasConfirmedRearIdentity: requiresRearView && rearIdentityIsValid(t, at: time),
            isRearConfirmationRetained: requiresRearView && rearIdentityIsValid(t, at: time)
                && t.vehicle.viewpoint?.hasConfirmedRearView(at: time) != true)
    }
    private func rearIdentityIsValid(_ t: Track, at time: TimeInterval) -> Bool {
        guard let verified = t.lastRearVerifiedAt, time >= verified else { return false }
        if time - verified <= rearConfirmationGraceSeconds { return true }
        // A confirmed identity need not reacquire rear classification while
        // fresh detections keep that same car in the measured driving path.
        // Only detector-backed path checks renew this continuity timestamp.
        guard let pathTime = t.lastPathVerifiedAt else { return false }
        return time >= pathTime && time - pathTime <= visualRetentionSeconds
    }
    private func canRetainRearIdentity(_ v: LeadVehicle, for t: Track, at time: TimeInterval) -> Bool {
        guard t.confirmed, let id = v.trackID, id == t.vehicle.trackID,
              rearIdentityIsValid(t, at: time) else { return false }
        let shapeChange = Double((v.box.width / v.box.height) / (t.vehicle.box.width / t.vehicle.box.height))
        return (0.50...2.0).contains(shapeChange)
    }
    private func canCoastInLane(_ t: Track, lane: LanePath?, at time: TimeInterval,
                                detections: [LeadVehicle], trackedBox: CGRect?) -> Bool {
        guard requiresRearView, t.confirmed, rearIdentityIsValid(t, at: time),
              trackedBox == nil, let lane, lane.isUsable(at: time), lane.confidence >= 0.70,
              let verified = t.lastPathVerifiedAt, time >= verified,
              time - verified <= laneRetentionSeconds,
              hasPathSupport(t.vehicle.box, lane: lane) else { return false }
        // A new overlapping object is not a disappearance caused by glare.
        return !detections.contains { $0.trackID != t.vehicle.trackID
            && Self.overlap($0.box, t.vehicle.box) >= 0.15 }
    }
    private func updateTrack(_ t: inout Track, with v: LeadVehicle, at time: TimeInterval,
                             lane: LanePath?, road: RoadRegion?) {
        let dt = time - t.lastDetection
        if dt > 0.01 {
            t.velocityX = 0.5 * t.velocityX + 0.5 * Double(v.box.midX - t.vehicle.box.midX) / dt
            t.velocityY = 0.5 * t.velocityY + 0.5 * Double(v.box.minY - t.vehicle.box.minY) / dt
        }
        t.vehicle = v; t.lastDetection = time; t.observations += 1
        t.confirmed = t.confirmed || (t.observations >= minimumObservations && time - t.started >= confirmationSeconds - 1e-6)
        if pathSupported(v, lane: lane, road: road, at: time) {
            t.lastPathVerifiedAt = time
        }
        if v.viewpoint?.hasConfirmedRearView(at: time) == true
            || (t.lastRearVerifiedAt != nil && v.viewpoint?.isRearFacing(at: time) == true) {
            // Keep the real classifier capture timestamp. Repeated detector
            // frames must not make old orientation evidence look new.
            if let captured = v.viewpoint?.capturedAt {
                t.lastRearVerifiedAt = max(t.lastRearVerifiedAt ?? captured, captured)
            }
        }
    }
    private mutating func updateChallenger(_ v: LeadVehicle, at time: TimeInterval,
                                          lane: LanePath?, road: RoadRegion?) {
        if var pending = challenger,
           let matched = association(pending, expected: prediction(pending, at: time), candidates: [v]).vehicle {
            updateTrack(&pending, with: matched, at: time, lane: lane, road: road); challenger = pending
        } else { challenger = makeTrack(v, at: time) }
    }
    private func prediction(_ t: Track, at time: TimeInterval) -> CGRect {
        let dt = min(0.20, max(0, time - t.lastDetection))
        return t.vehicle.box.offsetBy(dx: max(-0.05, min(0.05, t.velocityX * dt)),
                                     dy: max(-0.025, min(0.025, t.velocityY * dt)))
    }
    private func plausibleTrackedBox(_ box: CGRect, previous: CGRect) -> Bool {
        LeadVehicle.validBox(box)
            && (0.55...1.80).contains(Double(box.width / previous.width))
            && (0.55...1.80).contains(Double(box.height / previous.height))
            && abs(box.midX - previous.midX) < max(0.12, previous.width * 1.30)
            && abs(box.midY - previous.midY) < max(0.10, previous.height)
            && Self.overlap(box, previous) >= 0.12
    }
    private func association(_ t: Track, expected: CGRect, candidates: [LeadVehicle],
                             requiresSameID: Bool = true) -> Match {
        let matches = candidates.filter { v in
            v.label == t.vehicle.label
                && (!requiresSameID || v.trackID == nil || t.vehicle.trackID == nil || v.trackID == t.vehicle.trackID)
                && (0.60...1.65).contains(Double(v.box.width / t.vehicle.box.width))
                && (0.60...1.65).contains(Double(v.box.height / t.vehicle.box.height))
                && abs(v.box.midX - expected.midX) <= max(0.05, t.vehicle.box.width * 0.75)
                && abs(v.box.minY - expected.minY) <= max(0.04, t.vehicle.box.height * 0.60)
                && (Self.overlap(v.box, expected) >= 0.12 ||
                    abs(v.box.midX - expected.midX) < t.vehicle.box.width * 0.35)
        }.sorted { associationScore($0, expected) > associationScore($1, expected) }
        guard let best = matches.first else { return Match() }
        if matches.count > 1,
           associationScore(best, expected) - associationScore(matches[1], expected) < 0.10,
           Self.overlap(best.box, matches[1].box) < 0.75 { return Match(ambiguous: true) }
        return Match(vehicle: best)
    }
    private func associationScore(_ v: LeadVehicle, _ expected: CGRect) -> Double {
        Self.overlap(v.box, expected) - 0.8 * abs(Double(v.box.midX - expected.midX))
            / max(0.03, Double(expected.width)) + 0.05 * v.confidence
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        return Double(intersection.width * intersection.height /
            max(0.00001, a.width * a.height + b.width * b.height - intersection.width * intersection.height))
    }
}
