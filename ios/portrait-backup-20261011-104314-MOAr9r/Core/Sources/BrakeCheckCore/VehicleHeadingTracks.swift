import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Only distinct, recent classifier captures contribute to confirmation.
/// A front/side/uncertain capture breaks the consecutive rear-view evidence.
public struct VehicleViewpointHistory {
    private var predictions: [VehicleViewpoint] = []
    public init() {}
    public mutating func reset() { predictions = [] }
    @discardableResult
    public mutating func append(_ prediction: VehicleViewpoint) -> Bool {
        if let last = predictions.last, prediction.capturedAt <= last.capturedAt { return false }
        predictions.removeAll { prediction.capturedAt - $0.capturedAt > 1.60 }
        if !prediction.isRearFacing(at: prediction.capturedAt) { predictions = [] }
        predictions.append(prediction)
        if predictions.count > 8 { predictions.removeFirst(predictions.count - 8) }
        return true
    }
    public func latest(at time: TimeInterval) -> VehicleViewpoint? {
        guard let last = predictions.last, last.isFresh(at: time) else { return nil }
        let rear = predictions.reversed().prefix { prediction in
            time - prediction.capturedAt <= 1.60 && prediction.isRearFacing(at: prediction.capturedAt)
        }
        return VehicleViewpoint(latest: last, rearCount: rear.count,
                                rearSpan: rear.last.map { last.capturedAt - $0.capturedAt } ?? 0)
    }
}

/// Stable identities for orientation evidence. An asynchronous crop result is
/// delivered to the ID it was submitted for, never to the nearest later car.
/// Ambiguous crossings lose their evidence and require fresh classification.
public struct VehicleHeadingTracks {
    private struct Track {
        let id: UUID
        let startedAt: TimeInterval
        var box: CGRect
        let label: String
        var seenAt: TimeInterval
        var velocityX = 0.0
        var velocityY = 0.0
        var history = VehicleViewpointHistory()
    }
    private struct Pair { let detection: Int; let track: Int; let score: Double }
    private var tracks: [Track] = []
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { tracks = []; lastUpdate = nil }

    public mutating func update(_ detections: [LeadVehicle], at time: TimeInterval,
                                preferredID: UUID? = nil, trackedBox: CGRect? = nil,
                                allowNightClassAliases: Bool = false) -> [LeadVehicle] {
        guard time.isFinite else { reset(); return [] }
        if let prior = lastUpdate, time <= prior { reset(); return [] }
        lastUpdate = time
        tracks.removeAll { time - $0.seenAt > 3.25 }
        let vehicles = Array(detections.filter {
            LeadVehicle.validBox($0.box) && $0.confidence.isFinite && (0...1).contains($0.confidence)
        }.prefix(64))
        var pairs: [Pair] = []
        for (detection, vehicle) in vehicles.enumerated() {
            for (index, track) in tracks.enumerated() where Self.sameDetectorFamily(track.label, vehicle.label,
                                                                                  night: allowNightClassAliases) {
                let dt = min(0.20, max(0, time - track.seenAt))
                let predicted = track.box.offsetBy(dx: max(-0.06, min(0.06, track.velocityX * dt)),
                    dy: max(-0.04, min(0.04, track.velocityY * dt)))
                let visual = trackedBox.flatMap { box -> CGRect? in
                    guard track.id == preferredID, LeadVehicle.validBox(box),
                          Self.overlap(box, track.box) >= 0.12,
                          (0.50...2.0).contains(Double(box.width / track.box.width)),
                          (0.50...2.0).contains(Double(box.height / track.box.height)) else { return nil }
                    return box
                }
                let expected = visual ?? predicted
                let normalMatch = Self.matches(vehicle.box, expected, previous: visual ?? track.box)
                // A confirmed lead has a separate accurate visual tracker.
                // It may bridge a glare-induced change in the detector crop,
                // while both-direction ambiguity checks below still apply.
                let supportedMatch = visual.map { Self.matchesSupportedLead(vehicle.box, $0) } == true
                guard normalMatch || supportedMatch else { continue }
                let score = Self.overlap(vehicle.box, expected)
                    - 0.4 * abs(Double(vehicle.box.midX - expected.midX)) / max(0.035, Double(expected.width))
                pairs.append(Pair(detection: detection, track: index, score: score))
            }
        }
        // Check ambiguity in both directions before assigning anything. Two
        // detections competing for one track must not inherit its rear label.
        var ambiguousDetections: Set<Int> = []
        var ambiguousTracks: Set<Int> = []
        for index in vehicles.indices {
            let matches = pairs.filter { $0.detection == index }.sorted { $0.score > $1.score }
            if matches.count > 1, matches[0].score - matches[1].score < 0.12 {
                ambiguousDetections.insert(index)
                ambiguousTracks.formUnion(matches.map { $0.track })
            }
        }
        for index in tracks.indices {
            let matches = pairs.filter { $0.track == index }.sorted { $0.score > $1.score }
            if matches.count > 1, matches[0].score - matches[1].score < 0.12 {
                ambiguousTracks.insert(index)
                ambiguousDetections.formUnion(matches.map { $0.detection })
            }
        }
        for index in ambiguousTracks {
            let prior = tracks[index]
            // Invalidate crops already in flight before this crossing, too.
            tracks[index] = Track(id: prior.id, startedAt: time, box: prior.box,
                label: prior.label, seenAt: prior.seenAt)
        }
        var assigned: [Int: Int] = [:]
        var used: Set<Int> = []
        for pair in pairs.sorted(by: { $0.score > $1.score }) {
            guard !ambiguousDetections.contains(pair.detection), !ambiguousTracks.contains(pair.track),
                  assigned[pair.detection] == nil, !used.contains(pair.track) else { continue }
            assigned[pair.detection] = pair.track; used.insert(pair.track)
        }
        var result: [LeadVehicle] = []
        for (index, vehicle) in vehicles.enumerated() {
            let trackIndex: Int
            if let matched = assigned[index] {
                trackIndex = matched
                let oldShape = tracks[matched].box.width / tracks[matched].box.height
                let newShape = vehicle.box.width / vehicle.box.height
                if !(0.75...1.33).contains(Double(newShape / oldShape)) {
                    // Recheck orientation after a changed crop, while retaining
                    // the geometrically associated object ID and width scale.
                    // Earlier asynchronous crops cannot restore the old history.
                    tracks[matched] = Track(id: tracks[matched].id, startedAt: time, box: vehicle.box,
                        label: tracks[matched].label, seenAt: time)
                }
                let dt = time - tracks[matched].seenAt
                if dt > 0.01 {
                    tracks[matched].velocityX = 0.5 * tracks[matched].velocityX
                        + 0.5 * Double(vehicle.box.midX - tracks[matched].box.midX) / dt
                    tracks[matched].velocityY = 0.5 * tracks[matched].velocityY
                        + 0.5 * Double(vehicle.box.midY - tracks[matched].box.midY) / dt
                }
                tracks[matched].box = vehicle.box; tracks[matched].seenAt = time
            } else {
                trackIndex = tracks.count
                tracks.append(Track(id: UUID(), startedAt: time, box: vehicle.box,
                                    label: vehicle.label, seenAt: time))
            }
            let track = tracks[trackIndex]
            // YOLO can alternate car/truck for one SUV. Keep its original
            // category and assumed range scale while its identity is intact.
            // Geometry and crossing ambiguity still decide association.
            result.append(LeadVehicle(box: vehicle.box, label: track.label, confidence: vehicle.confidence,
                viewpoint: track.history.latest(at: time), trackID: track.id))
        }
        // Bound retained state during crowded scenes and repeated ambiguity.
        if tracks.count > 128 { tracks = Array(tracks.sorted { $0.seenAt > $1.seenAt }.prefix(128)) }
        return result
    }

    @discardableResult
    public mutating func record(_ prediction: VehicleViewpoint, for id: UUID, at time: TimeInterval) -> Bool {
        guard time.isFinite, prediction.isFresh(at: time), lastUpdate == time,
              let index = tracks.firstIndex(where: { $0.id == id }),
              time >= tracks[index].seenAt, time - tracks[index].seenAt <= 0.45 + 1e-6,
              tracks[index].startedAt <= prediction.capturedAt,
              prediction.capturedAt <= tracks[index].seenAt else { return false }
        // The crop came from a real detection of this ID. One missed later
        // detector frame must not discard its result; crossings and changed
        // crops still invalidate it through startedAt. Retained history alone
        // never appears as a fresh detection or acquires a new lead.
        return tracks[index].history.append(prediction)
    }
    public func viewpoint(for id: UUID, at time: TimeInterval) -> VehicleViewpoint? {
        tracks.first(where: { $0.id == id })?.history.latest(at: time)
    }
    private static func sameDetectorFamily(_ a: String, _ b: String, night: Bool) -> Bool {
        if a == b { return true }
        // ExDark often alternates car/bus for the very same silhouette. Keep
        // an existing category's range scale; never give a new bus rear proof.
        if night, ["car", "truck", "bus"].contains(a), ["car", "truck", "bus"].contains(b) { return true }
        return ["car", "truck"].contains(a) && ["car", "truck"].contains(b)
    }
    private static func matches(_ box: CGRect, _ expected: CGRect, previous: CGRect) -> Bool {
        (0.65...1.55).contains(Double(box.width / previous.width))
            && (0.65...1.55).contains(Double(box.height / previous.height))
            && abs(box.midX - expected.midX) <= max(0.04, previous.width * 0.45)
            && abs(box.midY - expected.midY) <= max(0.03, previous.height * 0.45)
            && overlap(box, expected) >= 0.35
    }
    private static func matchesSupportedLead(_ box: CGRect, _ visual: CGRect) -> Bool {
        (0.60...1.65).contains(Double(box.width / visual.width))
            && (0.60...1.65).contains(Double(box.height / visual.height))
            && abs(box.midX - visual.midX) <= visual.width * 0.40
            && abs(box.midY - visual.midY) <= visual.height * 0.40
            && overlap(box, visual) >= 0.25
    }
    private static func overlap(_ a: CGRect, _ b: CGRect) -> Double {
        let intersection = a.intersection(b)
        guard !intersection.isNull else { return 0 }
        let area = intersection.width * intersection.height
        return Double(area / max(0.00001, a.width * a.height + b.width * b.height - area))
    }
}
