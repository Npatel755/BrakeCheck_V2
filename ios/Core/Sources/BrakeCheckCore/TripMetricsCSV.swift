import Foundation

public enum TripMetricsCSV {
    public static func encode(_ trips: [TripMetrics]) -> String {
        let date = ISO8601DateFormatter()
        date.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var rows = ["trip_id,started_at,ended_at,duration_seconds,maximum_gps_speed_mph,estimated_lead_hard_brakes,estimated_closing_events_below_2_seconds,own_hard_brakes"]
        for trip in trips {
            let speed = trip.maxSpeedMPH.map { String(format: "%.2f", locale: Locale(identifier: "en_US_POSIX"), $0) } ?? ""
            rows.append([trip.id.uuidString, date.string(from: trip.startedAt), date.string(from: trip.endedAt),
                String(format: "%.2f", locale: Locale(identifier: "en_US_POSIX"), trip.durationSeconds), speed,
                String(trip.estimatedLeadHardBrakes), String(trip.estimatedCloseCalls),
                trip.ownHardBrakes.map(String.init) ?? ""].joined(separator: ","))
        }
        return rows.joined(separator: "\r\n") + "\r\n"
    }
}
