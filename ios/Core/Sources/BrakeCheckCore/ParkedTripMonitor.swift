import Foundation

/// What the trip screen should show once a trip has been parked for a long time.
public struct ParkedTripPrompt: Equatable, Sendable {
    public let secondsRemaining: Int
    public init(secondsRemaining: Int) { self.secondsRemaining = secondsRemaining }

    public var text: String { "Parked · trip ends in \(secondsRemaining) s" }
}

/// Ends a forgotten trip after the car has been parked for a while, so the camera, GPS and
/// recorder stop draining the battery. Only confirmed stationary fixes advance the park timer
/// and only confident movement resets it; an unavailable fix leaves it where it is. A prompt
/// with a countdown precedes the end so the driver can keep the trip going with one tap.
public struct ParkedTripMonitor: Equatable, Sendable {
    public enum Decision: Equatable, Sendable {
        case none
        case prompt(secondsRemaining: Int)
        case endTrip
    }

    public let parkDelay: TimeInterval
    public let promptDuration: TimeInterval
    private var stationarySince: TimeInterval?
    private var promptSince: TimeInterval?

    public init(parkDelay: TimeInterval = 8 * 60, promptDuration: TimeInterval = 90) {
        self.parkDelay = parkDelay
        self.promptDuration = promptDuration
    }

    public mutating func reset() {
        stationarySince = nil
        promptSince = nil
    }

    /// The driver asked to keep the trip: the park timer restarts from now.
    public mutating func keepDriving(at now: TimeInterval) {
        stationarySince = now
        promptSince = nil
    }

    /// - Parameters:
    ///   - stationary: `true` for a fresh stationary fix, `false` for confident movement,
    ///     `nil` when GPS cannot tell (the timer neither advances nor resets).
    ///   - hold: the trip must not end right now (guidance still running, a check-in open);
    ///     the park timer keeps counting but no prompt is shown.
    public mutating func update(stationary: Bool?, hold: Bool, at now: TimeInterval) -> Decision {
        switch stationary {
        case .some(false):
            reset()
            return .none
        case .some(true):
            if stationarySince == nil { stationarySince = now }
        case .none:
            break
        }
        guard let since = stationarySince, now - since >= parkDelay else { return .none }
        guard !hold else { promptSince = nil; return .none }
        let promptStart = promptSince ?? now
        promptSince = promptStart
        let remaining = promptDuration - (now - promptStart)
        guard remaining > 0 else {
            reset()
            return .endTrip
        }
        return .prompt(secondsRemaining: Int(remaining.rounded(.up)))
    }
}
