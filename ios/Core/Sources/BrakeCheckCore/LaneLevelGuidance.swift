import Foundation

/// Which way the next spoken maneuver goes, read from the provider instruction.
public enum LaneGuidanceManeuver: Equatable {
    case left, right, straight
    public init(instruction: String) {
        switch NavigationManeuver.symbol(for: instruction) {
        case "arrow.turn.up.left", "arrow.uturn.left": self = .left
        case "arrow.turn.up.right": self = .right
        default: self = .straight
        }
    }
}

public struct LaneGuidanceAdvice: Equatable {
    public enum Direction: Equatable { case left, right, stay }
    public let direction: Direction
    /// Painted lanes visible beside the vehicle, including the current one.
    public let lanesVisible: Int
    /// Zero-based position of the current lane among the visible lanes, from the left.
    public let laneIndex: Int
    public let text: String
    public let spoken: String?
    /// True exactly once per maneuver and direction, when the advice first becomes stable.
    public let shouldSpeak: Bool
    public let capturedAt: TimeInterval
    public var symbol: String {
        switch direction {
        case .left: return "arrow.left.to.line"
        case .right: return "arrow.right.to.line"
        case .stay: return "road.lanes"
        }
    }
}

/// Lane-level preparation for an upcoming turn or exit, from UFLDv2's four trained
/// boundary slots (0 = far left, 1/2 = current lane, 3 = far right). It is advice,
/// not a lane-change command: it never asks the driver to cross a solid line, never
/// suggests a left move across a continuous centre line (two-way roads), and requires
/// the same reading on several consecutive fresh observations before showing anything.
public struct LaneLevelGuidance {
    public static let minimumDistance = 60.0
    public static let minimumSpeed = 5.0
    public static let maximumAge = LanePredictionDisplay.maximumAge
    public static let requiredObservations = 3
    public static let requiredStableSeconds = 0.8

    private var proposal: LaneGuidanceAdvice.Direction?
    private var proposalSince: TimeInterval?
    private var proposalCount = 0
    private var lastObservationAt: TimeInterval?
    private var spoken: Set<String> = []
    private var current: (key: String, direction: LaneGuidanceAdvice.Direction)?
    public init() {}
    public mutating func reset() { self = Self() }

    public mutating func update(_ observation: LanePredictionObservation?, maneuver: LaneGuidanceManeuver,
                                maneuverKey: String, distance: Double?, speed: Double?,
                                navigating: Bool, now: TimeInterval) -> LaneGuidanceAdvice? {
        guard navigating, now.isFinite, maneuver != .straight,
              let distance, distance.isFinite, distance >= Self.minimumDistance,
              let speed, speed.isFinite, speed >= Self.minimumSpeed,
              distance <= max(250, min(900, speed * 25)),
              let observation, observation.capturedAt.isFinite, now >= observation.capturedAt,
              now - observation.capturedAt <= Self.maximumAge + 1e-9 else {
            clearProposal(); return nil
        }
        if let last = lastObservationAt {
            guard observation.capturedAt > last else { return nil }
            if observation.capturedAt - last > 0.6 { clearProposal() }
        }
        lastObservationAt = observation.capturedAt
        let slots = Self.slots(observation)
        guard let ego = slots[1], let egoRight = slots[2], ego.visible, egoRight.visible else {
            clearProposal(); return nil
        }
        let leftLane = slots[0]?.visible == true
        let rightLane = slots[3]?.visible == true
        let direction: LaneGuidanceAdvice.Direction
        switch maneuver {
        case .right: direction = rightLane && egoRight.dashed ? .right : .stay
        case .left: direction = leftLane && ego.dashed ? .left : .stay
        case .straight: return nil
        }
        if proposal != direction {
            proposal = direction; proposalSince = observation.capturedAt; proposalCount = 0
        }
        proposalCount += 1
        guard proposalCount >= Self.requiredObservations,
              observation.capturedAt - (proposalSince ?? observation.capturedAt) >= Self.requiredStableSeconds - 1e-9 else {
            return nil
        }
        let lanesVisible = 1 + (leftLane ? 1 : 0) + (rightLane ? 1 : 0)
        let laneIndex = leftLane ? 1 : 0
        let target = Self.target(maneuverKey)
        let text: String, spokenText: String?
        switch direction {
        case .right:
            text = "Move one lane right for the \(target), when safe"
            spokenText = "When it is safe, move one lane to the right for the \(target)."
        case .left:
            text = "Move one lane left for the \(target), when safe"
            spokenText = "When it is safe, move one lane to the left for the \(target)."
        case .stay:
            text = "Stay in this lane for the \(target)"
            spokenText = nil
        }
        let speechKey = maneuverKey + "|" + String(describing: direction)
        let shouldSpeak = spokenText != nil && !spoken.contains(speechKey)
        if shouldSpeak { spoken.insert(speechKey) }
        current = (maneuverKey, direction)
        return LaneGuidanceAdvice(direction: direction, lanesVisible: lanesVisible, laneIndex: laneIndex,
            text: text, spoken: spokenText, shouldSpeak: shouldSpeak, capturedAt: observation.capturedAt)
    }

    private mutating func clearProposal() {
        proposal = nil; proposalSince = nil; proposalCount = 0; lastObservationAt = nil; current = nil
    }

    private static func target(_ instruction: String) -> String {
        let text = instruction.lowercased()
        if text.contains("exit") { return "exit" }
        if text.contains("merge") || text.contains("keep") || text.contains("bear") { return "lane split" }
        return "turn"
    }

    struct Slot {
        var pieces: [(minY: Double, maxY: Double)] = []
        /// A usable boundary reaches the near field and spans a meaningful stretch of road.
        var visible: Bool {
            guard let nearest = pieces.map(\.minY).min(), let farthest = pieces.map(\.maxY).max() else { return false }
            return nearest <= 0.35 && farthest - nearest >= 0.08
        }
        /// Broken paint shows as separate pieces or incomplete coverage of its own span.
        /// One continuous piece covering almost its whole span is treated as a solid line.
        var dashed: Bool {
            guard let nearest = pieces.map(\.minY).min(), let farthest = pieces.map(\.maxY).max(),
                  farthest > nearest else { return false }
            if pieces.count >= 2 { return true }
            let covered = pieces.reduce(0) { $0 + ($1.maxY - $1.minY) }
            return covered / (farthest - nearest) < 0.75
        }
    }

    static func slots(_ observation: LanePredictionObservation) -> [Int: Slot] {
        var slots: [Int: Slot] = [:]
        for boundary in observation.boundaries where (0..<4).contains(boundary.laneSlot) {
            let ys = boundary.points.map(\.y).filter { $0.isFinite && (0...1).contains($0) }
            guard let low = ys.min(), let high = ys.max(), boundary.points.count >= 2 else { continue }
            slots[boundary.laneSlot, default: Slot()].pieces.append((low, high))
        }
        return slots
    }
}
