import Foundation

/// A 0–100 summary of how eventful a trip was, from hard brakes (the car ahead's and
/// the driver's own) and close calls per ten minutes of driving. Trips shorter than
/// two minutes have no score.
public struct TripSafetyScore: Equatable, Sendable {
    public let value: Int

    public init?(durationSeconds: Double, hardBrakes: Int, closeCalls: Int) {
        guard durationSeconds.isFinite, durationSeconds >= 120, hardBrakes >= 0, closeCalls >= 0 else { return nil }
        let tenMinuteBlocks = max(durationSeconds / 600, 0.5)
        let weightedEvents = Double(hardBrakes) + 2 * Double(closeCalls)
        let rate = weightedEvents / tenMinuteBlocks
        value = max(0, min(100, Int((100 - 12 * rate).rounded())))
    }

    public init?(_ trip: TripMetrics) {
        self.init(durationSeconds: trip.durationSeconds, hardBrakes: trip.totalHardBrakes,
                  closeCalls: trip.estimatedCloseCalls)
    }

    public var label: String {
        switch value {
        case 90...: return "Smooth"
        case 70..<90: return "Steady"
        case 50..<70: return "Busy"
        default: return "Rough"
        }
    }
}
