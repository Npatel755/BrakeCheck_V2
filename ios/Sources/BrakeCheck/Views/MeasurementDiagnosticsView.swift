import SwiftUI
import BrakeCheckCore

struct MeasurementDiagnosticsView: View {
    @ObservedObject var model: DriveModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("Vehicle measurements · \(model.measurementRevision)") {
                    Text(model.leadSelectionStatus)
                    LabeledContent("Lead vehicle ID", value: model.leadTargetID?.uuidString ?? "None")
                    Text(model.leadRearEvidenceStatus)
                    Text(model.cameraRange.reason)
                    LabeledContent("Camera gap", value: model.trackedDistance.map { String(format: "%.1f m", $0) } ?? "Unavailable")
                    LabeledContent("Gap closing", value: (model.risk.closingSpeed ?? model.cameraRange.closingSpeed).map { String(format: "%.2f m/s", $0) } ?? "Unavailable")
                    LabeledContent("Closure acceleration", value: model.cameraRange.closingAcceleration.map { String(format: "%.2f m/s²", $0) } ?? "Unavailable")
                    LabeledContent("Lead speed", value: model.risk.leadSpeed.map { String(format: "%.1f m/s", $0) } ?? model.speedBlocker)
                    LabeledContent("Lead speed continuity estimate", value: model.estimatedLeadSpeed.map { String(format: "~%.1f m/s · display only", $0) } ?? "Unavailable")
                    Text(model.frameAlignmentStatus)
                    Text(model.risk.reason)
                    Text("A confirmed lead uses fresh camera range and gap motion without needing lane detection or GPS. Closure acceleration describes the relative gap, not the lead's absolute acceleration. Stale or clipped measurements pause the numbers. A triggered brake warning remains visible for at least five seconds. It says BRAKE ALERT once current risk is no longer critical.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Camera and models") {
                    Picker("Detection lighting", selection: $model.detectorLightingMode) {
                        ForEach(DetectorLightingMode.allCases, id: \.self) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    Text(model.detectorLightingStatus)
                        .font(.caption)
                    Label(model.cameraStatus, systemImage: "video")
                    Label(model.detectorStatus, systemImage: "viewfinder")
                    Label(model.recorderStatus, systemImage: "record.circle")
                    Text(model.trackingStatus)
                }
                Section("GPS and impact sensing") {
                    Label(model.gpsStatus, systemImage: "location")
                    LabeledContent("GPS speed", value: model.speed.map { String(format: "%.2f m/s", $0) } ?? "Unavailable")
                    LabeledContent("GPS uncertainty", value: model.gpsAccuracy.map { String(format: "±%.2f m/s", $0) } ?? "Unavailable")
                    Text("Brief GPS gaps can show an estimated speed for up to 2.5 seconds from the last accepted fix, with growing uncertainty. A later reliable fix can interpolate an earlier camera frame. These estimates do not become measured GPS fixes, braking evidence, or logged speed. Current camera gap motion supplies danger independently.")
                        .font(.caption).foregroundStyle(.secondary)
                    Label(model.motionStatus, systemImage: "waveform.path")
                }
                Section("Experimental fusion · display only") {
                    Text(model.fusedSpeed.map { String(format: "%.1f mph", $0 * 2.2369362921) } ?? "Unavailable")
                    Text(model.fusedSpeedUncertainty.map { String(format: "Estimated uncertainty ±%.1f mph", $0 * 2.2369362921) } ?? "No fresh estimate")
                    Text(model.fusedSpeedSource)
                    Text(model.fusionStatus)
                    Text("Absolute lead speed uses GPS. Camera gap motion can supply collision indication independently. Experimental fused speed does not supply collision estimates.")
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
