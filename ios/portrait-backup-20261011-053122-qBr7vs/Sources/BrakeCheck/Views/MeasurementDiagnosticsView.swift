import SwiftUI

struct MeasurementDiagnosticsView: View {
    @ObservedObject var model: DriveModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("Vehicle measurements · \(model.measurementRevision)") {
                    Text(model.leadSelectionStatus)
                    Text(model.cameraRange.reason)
                    LabeledContent("Camera gap", value: model.trackedDistance.map { String(format: "%.1f m", $0) } ?? "Unavailable")
                    LabeledContent("Gap closing", value: (model.risk.closingSpeed ?? model.cameraRange.closingSpeed).map { String(format: "%.2f m/s", $0) } ?? "Unavailable")
                    LabeledContent("Lead speed", value: model.risk.leadSpeed.map { String(format: "%.1f m/s", $0) } ?? model.speedBlocker)
                    Text(model.frameAlignmentStatus)
                    Text(model.risk.reason)
                    Text("Lead speed uses GPS and path evidence. Collision hazards use a reliable closing gap in a measured lane or a projected corridor on observed road; target motion or braking is not required. Camera closing time can remain available without GPS.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Camera and models") {
                    Label(model.cameraStatus, systemImage: "video")
                    Label(model.detectorStatus, systemImage: "viewfinder")
                    Label(model.recorderStatus, systemImage: "record.circle")
                    Text(model.trackingStatus)
                }
                Section("GPS and impact sensing") {
                    Label(model.gpsStatus, systemImage: "location")
                    LabeledContent("GPS speed", value: model.speed.map { String(format: "%.2f m/s", $0) } ?? "Unavailable")
                    LabeledContent("GPS uncertainty", value: model.gpsAccuracy.map { String(format: "±%.2f m/s", $0) } ?? "Unavailable")
                    Label(model.motionStatus, systemImage: "waveform.path")
                }
                Section("Experimental fusion · display only") {
                    Text(model.fusedSpeed.map { String(format: "%.1f mph", $0 * 2.2369362921) } ?? "Unavailable")
                    Text(model.fusedSpeedUncertainty.map { String(format: "Estimated uncertainty ±%.1f mph", $0 * 2.2369362921) } ?? "No fresh estimate")
                    Text(model.fusedSpeedSource)
                    Text(model.fusionStatus)
                    Text("Collision estimates use GPS, not this experimental fused speed.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("This trip") {
                    LabeledContent("Duration", value: String(format: "%.1f min", model.tripMinutes))
                    LabeledContent("Estimated lead hard brakes", value: "\(model.hardBrakes)")
                    LabeledContent("Estimated TTC below 2 seconds", value: "\(model.closeCalls)")
                    LabeledContent("Maximum GPS speed", value: String(format: "%.0f mph", model.maxSpeedMPH))
                    LabeledContent("Your acceleration", value: model.egoAcceleration.map { String(format: "%.1f m/s²", $0) } ?? "—")
                    LabeledContent("Estimated lead acceleration", value: model.risk.leadAcceleration.map { String(format: "%.1f m/s²", $0) } ?? "—")
                }
            }
            .font(.subheadline)
            .navigationTitle("Diagnostics · \(model.measurementRevision)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .tint(BrakeTheme.accent)
    }
}
