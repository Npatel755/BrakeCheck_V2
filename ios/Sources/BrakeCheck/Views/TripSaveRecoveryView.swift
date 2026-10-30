import SwiftUI

struct TripSaveRecoveryView: View {
    let model: DriveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(model.pendingTripSaveCount > 0 ? "Trip save needs attention" : "Trip history unavailable",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.brake(.headline)).foregroundStyle(BrakeTheme.warning)
            if let error = model.tripHistoryError {
                Text(error).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            Text("Your existing history has been kept. Free storage if needed, then retry the save.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Button { model.retryTripHistorySave() } label: {
                Label(model.tripHistoryBusy ? "Saving..." : "Retry trip save", systemImage: "arrow.clockwise")
                    .frame(minHeight: 44)
            }
            .disabled(model.tripHistoryBusy || model.stopping)
            .accessibilityIdentifier("trip.retrySave")
        }.brakePanel()
    }
}
