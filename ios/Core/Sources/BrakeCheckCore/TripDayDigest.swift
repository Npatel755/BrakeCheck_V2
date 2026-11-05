import Foundation

/// Today's driving at a glance for the Home page: trips, minutes and the mean trip
/// score. Shown only once there are two or more trips, since one trip is already
/// the last-trip card.
public struct TripDayDigest: Equatable, Sendable {
    public let trips: Int
    public let drivingSeconds: Double
    public let averageScore: Int?

    public init?(_ trips: [TripMetrics], now: Date = Date(), calendar: Calendar = .current) {
        let today = trips.filter { calendar.isDate($0.startedAt, inSameDayAs: now) && $0.durationSeconds.isFinite }
        guard today.count >= 2 else { return nil }
        self.trips = today.count
        drivingSeconds = today.reduce(0) { $0 + $1.durationSeconds }
        let scores = today.compactMap { TripSafetyScore($0)?.value }
        averageScore = scores.isEmpty ? nil : Int((Double(scores.reduce(0, +)) / Double(scores.count)).rounded())
    }

    /// "3 trips · 1 h 20 min · score 92"
    public var line: String {
        let minutes = Int((drivingSeconds / 60).rounded())
        let time = minutes >= 60 ? "\(minutes / 60) h \(minutes % 60) min" : "\(minutes) min"
        var parts = ["\(trips) trips", time]
        if let averageScore { parts.append("score \(averageScore)") }
        return parts.joined(separator: " · ")
    }
}
