import SwiftUI

/// Risk changes update only this status region; warning delivery stays synchronous.
struct DrivingRiskStatus: View {
    let model: DriveModel
    @ObservedObject var safety: SafetyService
    @ObservedObject var premiumStore: PremiumStore
    let large: Bool
    var selecting = false
    private var critical: Bool { model.brakeWarningActive }
    private var running: Bool { model.tripActive || model.replaying || model.demo }

    var body: some View {
        VStack(spacing: 4) {
            RiskPill(risk: model.risk, critical: critical,
                 meter: model.dangerMeter, large: large,
                 unavailableTitle: !model.demo && !model.replaying && !model.detectorReady
                    ? (model.detectorStatus.contains("could not load") ? "Detection unavailable" : "Preparing detection") : nil)
            if let alert = model.drivingAssistAlert, premiumStore.isSubscribed,
               !critical && !safety.checkinPresented && running && !selecting {
                Label(alert.feature.alertText, systemImage: alert.feature.icon)
                    .font(.brake(size: large ? 14 : 12, weight: .semibold))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .foregroundStyle(BrakeTheme.text)
                    .background(BrakeTheme.cameraSurface, in: RoundedRectangle(cornerRadius: 14))
                    .accessibilityIdentifier("drivingAssistAlert")
                    .allowsHitTesting(false)
            }
            if !critical && !model.demo && !model.replaying && !model.detectorReady && model.detectorStatus.contains("could not load") {
                Button("Detection failed · tap for details") { model.message = model.detectorStatus }
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.caution)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(.black.opacity(0.65), in: Capsule())
            }
        }
    }

}

/// Sensor telemetry has no dependency on MapKit's representable updates.
struct NavigationRiskStatus: View {
    let model: DriveModel
    var body: some View {
        RiskPill(risk: model.risk, critical: model.brakeWarningActive,
                 meter: model.dangerMeter, large: false, unavailableTitle: riskUnavailableTitle)
    }

    /// While the camera isn't tracking, the risk pill says why instead of "No reading".
    private var riskUnavailableTitle: String? {
        guard model.tripActive, !model.demo, !model.replaying else { return nil }
        if model.stopping { return "Saving recording" }
        if !model.cameraReady || model.changingCameraGeometry {
            let status = model.cameraStatus.lowercased()
            if status.contains("denied") { return "Camera not allowed" }
            if model.changingCameraGeometry || status.contains("paused") || status.contains("interrupted") { return "Camera paused" }
            if status.contains("required") || status.contains("could not") || status.contains("unavailable") {
                return "Camera unavailable"
            }
            return "Camera starting"
        }
        if !model.detectorReady {
            return model.detectorStatus.contains("could not load") ? "Detection unavailable" : "Preparing detection"
        }
        return nil
    }

}
