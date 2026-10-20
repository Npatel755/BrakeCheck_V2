import BrakeCheckCore
import SwiftUI
import UIKit

/// Setup uses the existing camera behind this panel, never another preview layer.
struct CameraCalibrationControls: View {
    let model: DriveModel
    @ObservedObject var presentation: CameraARPresentation
    @Binding var cameraAR: Bool
    var liveCameraSetup = false
    var openCameraSetup: (() -> Void)? = nil
    var onCompleted: (() -> Void)? = nil
    @State private var cameraReady = false
    @State private var changingGeometry = false
    @State private var moving = false
    @State private var aligning = false
    @State private var completed = false
    @AppStorage("navigation.automaticCalibration") private var automaticCalibration = true
    @AppStorage("display.mph") private var useFeet = true
    @AppStorage("navigation.mountHeight") private var mountHeight = 1.4
    @AppStorage("navigation.horizonOffset") private var horizonOffset = 0.0
    @AppStorage("navigation.mountYaw") private var mountYaw = 0.0
    @AppStorage("navigation.verifiedMount") private var verifiedMount = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if completed {
                Label(model.cameraAlignmentVerified ? "Alignment locked" : "Mount saved", systemImage: "checkmark.circle.fill")
                    .font(.brake(.headline)).foregroundStyle(.green)
                Text("Returning to the camera. Road alignment finishes automatically when stable lanes appear; no further setup taps are needed.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            } else {
                if liveCameraSetup {
                    Label(model.cameraAlignmentVerified ? "Alignment locked" : model.cameraMountConfirmed ? "Waiting for road alignment" : "Set up your mount",
                          systemImage: model.cameraAlignmentVerified ? "lock.fill" : "viewfinder")
                        .font(.brake(.headline)).foregroundStyle(model.cameraAlignmentVerified ? .green : BrakeTheme.accentText)
                    Text("While parked, secure the phone facing forward and set the lens height below. Lane markings are not needed here. The app aligns to the road automatically later.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                } else {
                    Text("Camera alignment").font(.brake(.headline))
                    Text("Use Camera setup on the live road view. A saved alignment survives brief tracking gaps; reset it only when you change the mount.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
                LabeledContent("Lens above pavement", value: useFeet
                    ? String(format: "%.1f ft", mountHeight / 0.3048) : String(format: "%.2f m", mountHeight))
                Slider(value: Binding(get: { mountHeight }, set: { mountHeight = $0; settingsChanged() }),
                       in: 0.5...3, step: 0.025).disabled(aligning)
                    .accessibilityLabel("Camera lens height above pavement")
                Text("Measure from the lens down to the road, including the vehicle's height. Keep your measured value for this mount.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)

                if liveCameraSetup {
                    if aligning {
                        ProgressView("Saving mount...")
                    }
                    Button(aligning ? "Cancel" : model.cameraMountConfirmed ? "Done" : "I'm parked - save mount") {
                        if aligning { aligning = false; return }
                        // Reopening setup must not erase a verified orientation.
                        // A changed mount has its own explicit reset below.
                        if model.cameraMountConfirmed { completed = true; return }
                        guard !model.hasMeasuredMovement(at: ProcessInfo.processInfo.systemUptime) else { return }
                        // A new mount cannot inherit an orientation estimate
                        // learned from a different physical phone position.
                        model.requestAutomaticCameraCalibration()
                        aligning = true
                        finishIfReady()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!aligning && !model.cameraMountConfirmed
                        && (!cameraReady || changingGeometry || moving || model.demo || model.replaying))
                    if !cameraReady {
                        Text("Start a live trip to align the camera.").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    } else if moving {
                        Text("GPS indicates movement. Park before using setup.").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    } else if changingGeometry {
                        Text("Keep the phone orientation steady.").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    } else if aligning {
                        Text("Waiting for camera geometry. No lane markings are needed to save your mount.")
                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                    if model.cameraMountConfirmed {
                        Button("Phone mount moved? Reset alignment") {
                            model.requestAutomaticCameraCalibration()
                            completed = false
                        }
                        .font(.brake(.caption, weight: .semibold))
                        .disabled(!cameraReady || changingGeometry || moving || model.demo || model.replaying)
                    }
                } else if let openCameraSetup {
                    Button("Open live camera setup", action: openCameraSetup).disabled(!cameraReady || model.demo || model.replaying)
                    if !cameraReady { Text("Start a live trip first.").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted) }
                }

                DisclosureGroup("Advanced adjustments") {
                    Toggle("Estimate orientation automatically", isOn: $automaticCalibration)
                    if let estimate = presentation.calibrationEstimate {
                        LabeledContent("Saved horizon", value: String(format: "%.2f", estimate.horizon))
                        LabeledContent("Saved yaw", value: String(format: "%+.1f°", estimate.yawDegrees))
                    }
                    LabeledContent("Horizon adjustment", value: String(format: "%+.2f", horizonOffset))
                    Slider(value: Binding(get: { horizonOffset }, set: { horizonOffset = $0; settingsChanged() }), in: -0.15...0.15, step: 0.005)
                    LabeledContent("Mount yaw", value: String(format: "%+.0f°", mountYaw))
                    Slider(value: Binding(get: { mountYaw }, set: { mountYaw = $0; settingsChanged() }), in: -30...30, step: 1)
                    Button("Reset orientation (keep height)") {
                        horizonOffset = 0; mountYaw = 0; settingsChanged()
                        model.requestAutomaticCameraCalibration()
                    }
                }.disabled(aligning)
            }
        }
        .onReceive(model.$cameraReady.removeDuplicates()) { cameraReady = $0; finishIfReady() }
        .onReceive(model.$captureGeometry) { _ in finishIfReady() }
        .onReceive(model.$changingCameraGeometry.removeDuplicates()) { changingGeometry = $0 }
        .onReceive(model.$speed.map { _ in model.hasMeasuredMovement(at: ProcessInfo.processInfo.systemUptime) }.removeDuplicates()) { moving = $0 }
        // Setup needs perception while it is visible, independent of whether
        // the driver has enabled the on-road navigation ribbon.
        .onAppear { model.setARCalibrationVisible(liveCameraSetup) }
        .onChange(of: automaticCalibration) { _, enabled in
            if enabled && !model.cameraMountConfirmed { model.requestAutomaticCameraCalibration() }
        }
        .onChange(of: presentation.scene?.capturedAt) { _, _ in finishIfReady() }
        .onChange(of: presentation.calibrationEstimate) { _, _ in finishIfReady() }
        .onChange(of: moving) { _, knownMoving in if knownMoving { aligning = false } }
        .onDisappear { model.setARCalibrationVisible(false) }
        .task(id: completed) {
            guard completed else { return }
            do { try await Task.sleep(for: .milliseconds(650)) } catch { return }
            onCompleted?()
        }
    }
    private func settingsChanged() {
        verifiedMount = ""; presentation.setAlignmentLocked(false)
        UserDefaults.standard.removeObject(forKey: "navigation.confirmedMount")
        presentation.setMountConfirmed(false)
        aligning = false; completed = false
    }
    private func finishIfReady() {
        guard liveCameraSetup, aligning, !completed,
              model.confirmCameraMount(parkedAcknowledged: true) else { return }
        aligning = false; completed = true
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        UIAccessibility.post(notification: .announcement, argument: "Mount saved. Road alignment will finish automatically.")
    }
}

/// An in-place card keeps the original capture preview attached and visible.
struct CameraCalibrationOverlay: View {
    let model: DriveModel
    @ObservedObject var presentation: CameraARPresentation
    @Binding var cameraAR: Bool
    var onClose: () -> Void
    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .bottom) {
                Color.clear.contentShape(Rectangle()).onTapGesture { }
                VStack(spacing: 8) {
                    HStack {
                        Text("Camera setup").font(.brake(.headline))
                        Spacer()
                        Button("Close", systemImage: "xmark.circle.fill", action: onClose).labelStyle(.iconOnly)
                    }
                    ScrollView {
                        CameraCalibrationControls(model: model, presentation: presentation, cameraAR: $cameraAR,
                            liveCameraSetup: true, onCompleted: onClose)
                    }
                }
                .padding(16).frame(maxWidth: 520)
                .frame(height: min(geometry.size.height * 0.65, 390))
                .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18))
                .padding(12)
            }
        }
    }
}

struct CameraAlignmentShortcut: View {
    @ObservedObject var presentation: CameraARPresentation
    var onOpen: () -> Void
    var body: some View {
        Button(action: onOpen) {
            Label(presentation.alignmentLocked ? "Alignment locked" : presentation.mountConfirmed ? "Mount saved" : "Camera setup",
                  systemImage: presentation.alignmentLocked ? "lock.fill" : "viewfinder")
        }
        .font(.brake(.caption, weight: .semibold)).foregroundStyle(presentation.alignmentLocked ? .green : BrakeTheme.accentText)
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(BrakeTheme.cameraSurface, in: Capsule())
    }
}
