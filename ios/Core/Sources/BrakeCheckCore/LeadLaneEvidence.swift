import Foundation

/// Independently confirmed, unsmoothed ego-pair observations for lead selection.
/// The painted camera polygon is not evidence by itself. This path requires
/// both ego heads, adequate observed span and three distinct consistent captures.
/// It cannot be constructed from held display output or single-sided paint.
public struct LeadLaneEvidenceEstimator {
    private var previous: LanePath?
    private var lastTime: TimeInterval?
    private var began: TimeInterval?
    private var count = 0
    public private(set) var status = "waiting for lane pair"
    public init() {}
    public mutating func reset() {
        previous = nil; lastTime = nil; began = nil; count = 0
        status = "waiting for lane pair"
    }

    public mutating func update(_ observation: LanePredictionObservation?, at time: TimeInterval) -> LanePath? {
        guard time.isFinite, lastTime.map({ time > $0 }) ?? true else { reset(); return nil }
        if let lastTime, time - lastTime > 0.65 { reset() }
        lastTime = time
        guard let observation, observation.capturedAt == time,
              let visible = LanePredictionDisplay.visible(observation, at: time) else { return reject() }
        // A weak fragment invalidates only that section. No model softmax is
        // claimed to be calibrated geometric certainty.
        let strong = visible.boundaries.filter { (1...2).contains($0.laneSlot)
            && $0.confidence >= 0.82 && $0.points.count >= 8 }
        let sections = LaneCorridorDisplay.sections(.init(capturedAt: time, boundaries: strong), at: time)
        let candidates = sections.compactMap { section -> LanePath? in
            guard section.left.count >= 8, let near = section.left.first, let far = section.left.last,
                  far.y - near.y >= 0.14 else { return nil }
            let bands = zip(section.left, section.right).map { LaneBand(y: $0.y, left: $0.x, right: $1.x) }
            guard let first = bands.first,
                  bands.allSatisfy({ $0.right - $0.left <= first.right - first.left + 0.10 }),
                  zip(bands, bands.dropFirst()).allSatisfy({ a, b in
                      max(abs(a.left - b.left), abs(a.right - b.right)) <= 3.5 * (b.y - a.y)
                  }) else { return nil }
            let confidence = strong.map(\.confidence).min() ?? 0
            let path = LanePath(bands: bands, confidence: confidence, capturedAt: time, source: .neural)
            return path.isUsable(at: time) ? path : nil
        }
        // Multiple separated candidate sections are not joined. Pick only an
        // unambiguous longest measured span; tied competing spans stay unknown.
        let ordered = candidates.sorted { span($0) > span($1) }
        guard let path = ordered.first else { return reject() }
        if ordered.count > 1, abs(span(path) - span(ordered[1])) < 0.03 { return reject() }
        var consistent = false
        if let old = previous {
            let shifts = path.bands.compactMap { band -> Double? in
                guard let bounds = old.bounds(at: band.y) else { return nil }
                return max(abs(band.left - bounds.left), abs(band.right - bounds.right))
            }
            consistent = shifts.count >= 6 && shifts.reduce(0, +) / Double(shifts.count) <= 0.035
                && (shifts.max() ?? 1) <= 0.06
        }
        if !consistent { count = 0; began = time }
        previous = path; count += 1
        guard count >= 3, let began, time - began >= 0.30 - 1e-6 else {
            status = "confirming lane pair \(min(count, 3))/3"; return nil
        }
        status = "confirmed observed lane pair"
        return path // No averaging, extrapolation, new timestamp or inherited confidence.
    }

    private func span(_ path: LanePath) -> Double { path.bands.last!.y - path.bands.first!.y }
    private mutating func reject() -> LanePath? {
        previous = nil; count = 0; began = nil; status = "waiting for lane pair"; return nil
    }
}

/// Capture-ordered publication. A negative frame invalidates evidence from its
/// capture time, while older vehicle frames can still read their matching past.
public struct LeadLaneEvidenceBuffer {
    private struct Entry { let time: TimeInterval; let path: LanePath?; let boundaries: LanePredictionObservation? }
    private var entries: [Entry] = []
    public init() {}
    public mutating func reset() { entries = [] }
    public mutating func record(_ path: LanePath?, boundaries: LanePredictionObservation? = nil, at time: TimeInterval) {
        guard time.isFinite, entries.last.map({ time > $0.time }) ?? true,
              path.map({ $0.capturedAt == time && $0.isUsable(at: time) }) ?? true else { return }
        entries.removeAll { time - $0.time > 1.2 }
        entries.append(Entry(time: time, path: path, boundaries: boundaries.flatMap {
            $0.capturedAt == time ? LanePredictionDisplay.visible($0, at: time) : nil
        }))
        if entries.count > 12 { entries.removeFirst(entries.count - 12) }
    }
    public func latest(at time: TimeInterval) -> LanePath? {
        guard time.isFinite, let entry = entries.last(where: { $0.time <= time }),
              let path = entry.path, path.isUsable(at: time) else { return nil }
        return path
    }
    public func latestBoundaries(at time: TimeInterval) -> LanePredictionObservation? {
        guard time.isFinite, let entry = entries.last(where: { $0.time <= time }) else { return nil }
        return LanePredictionDisplay.visible(entry.boundaries, at: time)
    }
}
