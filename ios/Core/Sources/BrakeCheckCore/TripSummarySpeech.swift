import Foundation

/// One spoken sentence when a trip is saved, so the driver hears the outcome without
/// looking at the phone: "Trip saved. 24 minutes. Smooth, 94."
public enum TripSummarySpeech {
    public static func text(for trip: TripMetrics, language: String) -> String {
        let minutes = max(1, Int((trip.durationSeconds / 60).rounded()))
        let spanish = language == "es"
        var parts = [spanish ? "Viaje guardado." : "Trip saved.",
                     spanish ? (minutes == 1 ? "1 minuto." : "\(minutes) minutos.")
                             : (minutes == 1 ? "1 minute." : "\(minutes) minutes.")]
        if let score = TripSafetyScore(trip) {
            let label: String
            if spanish {
                switch score.label {
                case "Smooth": label = "Suave"
                case "Steady": label = "Estable"
                case "Busy": label = "Movido"
                default: label = "Brusco"
                }
            } else { label = score.label }
            parts.append("\(label), \(score.value).")
        }
        return parts.joined(separator: " ")
    }
}
