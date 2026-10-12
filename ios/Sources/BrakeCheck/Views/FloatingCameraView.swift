import BrakeCheckCore
import SwiftUI

/// A visual window into the existing capture session. Showing or hiding it never starts or stops a trip.
struct FloatingCameraView: View {
    @ObservedObject var model: DriveModel
    var showDetections: Bool
    var showLanes: Bool
    var onOpenCamera: () -> Void
    var onClose: () -> Void
    /// The host can shrink its frame to the header when the preview is collapsed.
    var onCollapsedChanged: ((Bool) -> Void)? = nil
    @State private var collapsed = false

    private var live: Bool {
        model.cameraReady && !model.changingCameraGeometry && !model.demo && !model.replaying
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            if !collapsed {
                GeometryReader { geometry in
                    ZStack {
                        Color.black
                        if live {
                            alignedCamera(in: geometry.size)
                        } else {
                            placeholder
                        }
                    }
                }
                .clipped()
            }
        }
        .background(BrakeTheme.panel)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay {
            RoundedRectangle(cornerRadius: 18)
                .strokeBorder(model.brakeWarningActive ? BrakeTheme.danger : Color.white.opacity(0.14),
                              lineWidth: model.brakeWarningActive ? 3 : 1)
                .allowsHitTesting(false)
        }
        .shadow(color: model.brakeWarningActive ? BrakeTheme.danger.opacity(0.4) : Color.black.opacity(0.5), radius: 12, y: 5)
        .foregroundStyle(.white)
        .tint(BrakeTheme.accent)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Floating camera window")
    }

    private var header: some View {
        HStack(spacing: 0) {
            HStack(spacing: 6) {
                Circle().fill(model.brakeWarningActive ? BrakeTheme.danger : live ? BrakeTheme.accent : BrakeTheme.muted)
                    .frame(width: 5, height: 5)
                Text(model.brakeWarningActive ? "BRAKE" : live ? "LIVE" : "PAUSED")
                    .font(.system(size: 10, weight: .bold)).tracking(0.8).lineLimit(1)
                    .foregroundStyle(model.brakeWarningActive ? BrakeTheme.danger : live ? BrakeTheme.accent : BrakeTheme.muted)
            }
            .padding(.leading, 12)
            Spacer(minLength: 2)
            if let onCollapsedChanged {
                headerButton(collapsed ? "chevron.down" : "chevron.up",
                             label: collapsed ? "Expand floating camera" : "Collapse floating camera") {
                    collapsed.toggle()
                    onCollapsedChanged(collapsed)
                }
            }
            headerButton("arrow.up.left.and.arrow.down.right", label: "Open full Camera page", action: onOpenCamera)
            headerButton("xmark", label: "Hide floating camera", action: onClose)
                .accessibilityHint("Your camera trip keeps running.")
        }
        .frame(height: 44)
        .background(BrakeTheme.panel)
    }

    private func headerButton(_ image: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: image)
                .font(.system(size: 12, weight: .semibold))
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func alignedCamera(in size: CGSize) -> some View {
        let frame = CameraGeometry.aspectFitRect(in: size, imageAspect: model.captureGeometry.aspect)
        return ZStack {
            CameraPreview(session: model.camera.session, orientation: model.captureGeometry.videoOrientation)
            if showLanes {
                RoadAreaOverlay(road: model.roadRegion)
                LeadCorridorOverlay(lane: model.lanePath)
            }
            if showDetections {
                DetectionOverlay(detections: model.detections, hazardTargetID: model.collisionHazardID,
                                 leadTargetID: model.leadTargetID)
            }
            TargetOverlay(trackedBox: model.trackingBox, selecting: false, selectedBox: .constant(nil),
                          alert: model.brakeWarningActive, coasting: model.trackingCoasting)
        }
        .frame(width: frame.width, height: frame.height)
        .position(x: frame.midX, y: frame.midY)
        .allowsHitTesting(false)
    }

    private var placeholder: some View {
        VStack(spacing: 6) {
            Image(systemName: model.replaying ? "play.rectangle" : "video.slash")
                .font(.system(size: 23, weight: .light))
                .foregroundStyle(BrakeTheme.muted)
            Text(placeholderTitle)
                .font(.system(size: 12, weight: .semibold))
            Text(placeholderDetail)
                .font(.system(size: 10))
                .foregroundStyle(BrakeTheme.muted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var placeholderTitle: String {
        if model.demo { return "Simulated drive" }
        if model.replaying { return "Replay is active" }
        if model.changingCameraGeometry { return "Camera adjusting" }
        return model.tripActive ? "Camera paused" : "Camera is off"
    }

    private var placeholderDetail: String {
        if model.demo { return "The Camera page shows sample readings." }
        if model.replaying { return "Open the Camera page to see your recording." }
        if model.changingCameraGeometry { return "Updating the road view." }
        return model.tripActive ? "Waiting for the live camera." : "Start a trip to see the road."
    }
}
