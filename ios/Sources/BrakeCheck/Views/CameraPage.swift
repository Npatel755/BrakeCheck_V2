import BrakeCheckCore
import SwiftUI
import UIKit

/// Keep video, boxes, and readouts in their own observation scope. A capture
/// must not rebuild the pager, Home controls, or navigation map.
struct CameraPage: View {
    let model: DriveModel
    @ObservedObject var navigation: NavigationService
    @ObservedObject var safety: SafetyService
    @ObservedObject var premiumStore: PremiumStore
    let portrait: Bool
    let cameraVisible: Bool
    @Binding var showCameraSetup: Bool
    @Binding var selecting: Bool
    @Binding var selection: CGRect?
    @Binding var measuredDistance: String
    let practiceActive: Bool
    let endPractice: () -> Void
    @Environment(\.colorScheme) private var colorScheme
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    @AppStorage("display.detectionBoxes") private var showDetections = true
    @AppStorage("display.laneGuides") private var showLanes = false
    @AppStorage("navigation.cameraAR") private var cameraAR = true
    @AppStorage("display.fullCameraFrame") private var showFullFrame = true
    @AppStorage("display.premiumCompactBanner") private var premiumCompactBanner = false
    @AppStorage("diagnostics.developerOverlays") private var developerOverlays = false

    private var factor: Double { useMPH ? 2.2369362921 : 3.6 }
    private var unit: String { useMPH ? "mph" : "km/h" }
    private var calibration: Double? { Double(measuredDistance.replacingOccurrences(of: ",", with: ".")) }
    /// A triggered warning lasts at least five seconds, even if the target or
    /// current risk disappears. Numeric measurements keep their own expiry.
    private var critical: Bool { model.brakeWarningActive }
    private var running: Bool { model.tripActive || model.replaying || model.demo }
    private var showsPlaceholder: Bool { (!model.cameraReady && !model.replaying) || model.demo }
    private var gapMotionBlocker: String {
        let reason = model.cameraRange.reason.lowercased()
        if reason.contains("gap ·") { return "Detection gap" }
        if reason.contains("delayed") || reason.contains("expired") { return "Camera delayed" }
        if reason.contains("clipped") { return "Vehicle clipped" }
        if model.trackedDistance == nil { return "Needs target" }
        if reason.contains("confidence") { return "Low confidence" }
        if reason.contains("unsettled") { return "Range unsettled" }
        return "Sampling"
    }

    var body: some View {
        ZStack {
            roadView(portrait: portrait).ignoresSafeArea()
            if showsPlaceholder { placeholder }
            if colorScheme == .dark { scrims(portrait: portrait).ignoresSafeArea().allowsHitTesting(false) }
            if portrait { portraitHUD } else { landscapeHUD }
            if cameraVisible && navigation.isNavigating && !critical && !selecting {
                VStack {
                    Spacer()
                    NavigationCameraGuidanceView(navigation: navigation, useMPH: useMPH, showsInstruction: false)
                        .padding(.horizontal, portrait ? 14 : 24)
                        .padding(.bottom, portrait ? 100 : 64)
                }
                .frame(maxWidth: .infinity, alignment: .trailing)
                .allowsHitTesting(false)
            }
            if developerOverlays && cameraAR && model.cameraReady && !model.replaying && !model.demo && !selecting {
                CameraRoadMaskStatus(presentation: model.arPresentation, portrait: portrait)
            }
        }
        .overlay {
            if practiceActive {
                PracticeDriveBanner(end: endPractice)
                    .frame(maxWidth: 420)
                    .offset(y: portrait ? 70 : 50)
            }
        }
        .clipShape(SideClip())
    }

    private func riskPill(large: Bool) -> some View {
        DrivingRiskStatus(model: model, safety: safety, premiumStore: premiumStore, large: large, selecting: selecting)
    }

    // MARK: - Road

    /// The camera (or replay frame) and its overlays share one box with the video's shape. The box fills the
    /// screen, fitting the whole camera frame by default; Home also offers a cropped fill view.
    private func roadView(portrait: Bool) -> some View {
        let frameSize = model.replaying ? model.replayFrame?.size : nil
        let aspect = frameSize.map { Double($0.width / max($0.height, 1)) } ?? model.captureGeometry.aspect
        return GeometryReader { geometry in
            let frame = showFullFrame
                ? CameraGeometry.aspectFitRect(in: geometry.size, imageAspect: aspect)
                : CameraGeometry.aspectFillRect(in: geometry.size, imageAspect: aspect)
            ZStack {
                BrakeTheme.background
                if model.replaying {
                    if let frame = model.replayFrame { Image(uiImage: frame).resizable().scaledToFit() }
                } else {
                    CameraPreview(session: model.camera.session, orientation: model.captureGeometry.videoOrientation)
                }
                if (model.cameraReady || model.replaying) && !model.demo && !model.changingCameraGeometry {
                    if developerOverlays && cameraVisible && cameraAR && !model.replaying && !selecting {
                        CameraRoadMaskOverlay(presentation: model.arPresentation)
                    }
                    if cameraVisible && !model.replaying && !selecting {
                        CameraNavigationOverlay(navigation: navigation, presentation: model.arPresentation, geometry: model.captureGeometry)
                    }
                    if showLanes && !selecting {
                        CameraPerceptionOverlay(presentation: model.arPresentation, lane: model.lanePath,
                            navigationOnCamera: cameraVisible && navigation.isNavigating && cameraAR)
                    }
                    if showDetections && !selecting {
                        DetectionOverlay(detections: model.detections, hazardTargetID: model.collisionHazardID,
                                         leadTargetID: model.leadTargetID)
                    }
                    TargetOverlay(trackedBox: model.trackingBox, selecting: selecting, selectedBox: $selection,
                                  alert: model.risk.level == .critical, coasting: model.trackingCoasting)
                }
            }
            .frame(width: frame.width, height: frame.height)
            .position(x: frame.midX, y: frame.midY)
        }
        .clipped()
    }

    private var placeholder: some View {
        let text = placeholderText
        return ZStack {
            BrakeTheme.panel.ignoresSafeArea()
            VStack(spacing: 10) {
                Image(systemName: model.demo ? "car.side.hill.up.fill" : "car.front.waves.up.fill")
                    .font(.brake(size: 44, weight: .light)).foregroundStyle(BrakeTheme.accentText.opacity(0.7))
                Text(text.title)
                    .font(.brake(size: 22, weight: .semibold))
                if model.tripActive && !model.demo {
                    Button { model.retryCamera() } label: {
                        Label("Retry camera", systemImage: "arrow.clockwise").frame(minHeight: 44)
                    }
                    .disabled(model.stopping)
                }
                Text(text.detail)
                    .font(.brake(size: 13)).foregroundStyle(BrakeTheme.muted).multilineTextAlignment(.center)
            }
            .padding(.horizontal, 24)
            .offset(y: -24)
        }
    }

    /// During a trip the camera is starting (or can't), so "Start a trip" would be wrong there.
    private var placeholderText: (title: String, detail: String) {
        if model.demo { return ("Simulated drive", "Practice numbers · no camera") }
        guard model.tripActive else {
            return ("A clearer view ahead.", "Start a trip from Home to see the road and track vehicles.")
        }
        let status = model.cameraStatus
        if status.localizedCaseInsensitiveContains("denied") {
            return ("Camera access is off", "Allow Camera for BrakeSense in iPhone Settings to see the road and track vehicles.")
        }
        if status.hasPrefix("Camera: ") || status.localizedCaseInsensitiveContains("could not") {
            return ("Camera unavailable", String(status.dropFirst(status.hasPrefix("Camera: ") ? 8 : 0)))
        }
        return ("Starting the camera…", "Keep the phone steady in its mount.")
    }

    /// Theme-matched gradients keep readouts legible over the live road.
    private func scrims(portrait: Bool) -> some View {
        VStack(spacing: 0) {
            LinearGradient(colors: [BrakeTheme.cameraScrim.opacity(0.6), .clear], startPoint: .top, endPoint: .bottom).frame(height: portrait ? 190 : 130)
            Spacer(minLength: 0)
            LinearGradient(colors: [.clear, BrakeTheme.cameraScrim.opacity(0.7)], startPoint: .top, endPoint: .bottom).frame(height: portrait ? 260 : 150)
        }
    }

    // MARK: - Layouts

    /// Text directions stay available when the optional road ribbon is off.
    private var showsDirections: Bool { cameraVisible && navigation.isNavigating && !selecting }

    private func directionsBanner(large: Bool) -> some View {
        NavigationBanner(navigation: navigation, presentation: model.arPresentation,
            compact: critical || (premiumStore.isSubscribed && premiumCompactBanner),
            large: large, onSetup: { showCameraSetup = true })
    }

    private var landscapeHUD: some View {
        VStack(spacing: 8) {
            if showsDirections {
                HStack(alignment: .top, spacing: 12) {
                    directionsBanner(large: false).frame(maxWidth: 400, alignment: .leading)
                    Spacer(minLength: 0)
                    if critical || running { riskPill(large: false).padding(.top, 4) }
                }
            } else {
                HStack(alignment: .top, spacing: 8) {
                    brand(size: 25)
                    Spacer(minLength: 0)
                    cameraSetupShortcut
                }
                if critical || (running && !selecting) { riskPill(large: false) }
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls.frame(maxWidth: 440) }
            // Standby shows only the welcome; readouts appear once there is something to read.
            if running { bottomRow(portrait: false) }
        }
        .padding(.horizontal, 12).padding(.top, 12).padding(.bottom, 6)
        .foregroundStyle(BrakeTheme.text)
    }

    /// Vertical: status and the danger meter above an unobstructed road, readouts below.
    private var portraitHUD: some View {
        VStack(spacing: 14) {
            if showsDirections {
                if critical || (running && !selecting) {
                    riskPill(large: true)
                }
                directionsBanner(large: true)
            } else {
                HStack(spacing: 8) {
                    brand(size: 19)
                    Spacer(minLength: 0)
                    cameraSetupShortcut
                }
                if critical || (running && !selecting) {
                    riskPill(large: true)
                }
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls }
            if running { bottomRow(portrait: true) }
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
        .foregroundStyle(BrakeTheme.text)
    }

    // MARK: - Controls

    @ViewBuilder private var cameraSetupShortcut: some View {
        if cameraVisible && running && !model.replaying && !model.demo && !selecting && !navigation.isNavigating {
            CameraAlignmentShortcut(presentation: model.arPresentation, onOpen: { showCameraSetup = true })
        }
    }

    private func brand(size: CGFloat) -> some View {
        HStack(spacing: 10) {
            BrakeLogo(size: size)
            statusPill
        }
        .modifier(CameraPlate(shape: AnyShape(Capsule()), padding: EdgeInsets(top: 4, leading: 12, bottom: 4, trailing: 4)))
        .fixedSize()
    }

    private var statusPill: some View {
        let (text, color): (String, Color) =
            model.demo ? ("SIMULATED", BrakeTheme.warning)
            : model.replaying ? ("REPLAY", BrakeTheme.warning)
            : model.tripActive ? (model.cameraReady ? "LIVE" : "CAMERA STARTING", model.cameraReady ? BrakeTheme.danger : BrakeTheme.muted)
            : ("STANDBY", BrakeTheme.muted)
        return HStack(spacing: 5) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(developerOverlays ? "\(text) · \(model.measurementRevision)" : text)
                .font(.brake(size: 10, weight: .bold)).tracking(1)
        }
        .padding(.horizontal, 10).frame(height: 26)
        .background(BrakeTheme.cameraSurface, in: Capsule())
    }

    // MARK: - Readouts

    /// Speed bottom-left and the car-ahead numbers bottom-right, so the middle of the road (where the lead car is) stays clear.
    private func bottomRow(portrait: Bool) -> some View {
        HStack(alignment: .bottom, spacing: 12) {
            speedReadout(size: portrait ? 58 : 64)
            Spacer(minLength: 0)
            carAheadNumbers(compact: portrait)
        }
    }

    private func speedReadout(size: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(model.gpsSpeedEstimated ? "EST. SPEED" : "SPEED")
                .font(.brake(size: 10, weight: .bold)).tracking(1).foregroundStyle(BrakeTheme.muted)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(model.speed.map { String(format: "%.0f", $0 * factor) } ?? "--")
                    .font(.brake(size: size, weight: .semibold)).monospacedDigit().tracking(-2)
                    .foregroundStyle(model.speed == nil ? BrakeTheme.muted : BrakeTheme.text)
                Text(unit).font(.brake(size: 15, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            }
            if model.replaying {
                Text(model.replaySpeedSource.hasPrefix("ASSUMED") ? "ASSUMED SPEED" : "RECORDED SPEED")
                    .font(.brake(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
            } else if model.gpsSpeedEstimated {
                Text("GPS GAP · ESTIMATED").font(.brake(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
            }
        }
        .modifier(CameraPlate())
        .shadow(color: colorScheme == .dark ? .black.opacity(0.5) : .clear, radius: 6)
    }

    /// Numbers float on the video like a head-up display; the bottom scrim and a shadow keep them readable.
    /// Feet and mph when the speed unit is mph, metres and km/h otherwise.
    private func carAheadNumbers(compact: Bool) -> some View {
        let risk = model.risk
        let cameraDistance = model.demo ? risk.distance : model.trackedDistance
        let distance = cameraDistance.map { useMPH ? $0 * 3.28084 : $0 }
        let distanceTitle = model.demo ? "Distance" : model.rangeSource.hasPrefix("CALIBRATED RANGE")
            ? "Calibrated gap" : "Approx. gap"
        let ttc = risk.timeToCollision
        let closing = model.demo ? risk.closingSpeed : model.collisionHazardID != nil
            ? risk.closingSpeed ?? model.cameraRange.closingSpeed
            : model.cameraRange.closingSpeed ?? risk.closingSpeed
        let size: CGFloat = compact ? 21 : 24
        return Grid(alignment: .trailing, horizontalSpacing: compact ? 16 : 22, verticalSpacing: 8) {
            GridRow {
                hudNumber(distanceTitle, distance.map { String(format: "%.0f", $0) }, useMPH ? "ft" : "m", size: size)
                hudNumber(risk.leadSpeed == nil && model.estimatedLeadSpeed != nil ? "Est. their speed" : "Their speed",
                          (risk.leadSpeed ?? model.estimatedLeadSpeed).map { String(format: "%.0f", $0 * factor) }, unit,
                          size: size, unavailable: model.demo ? nil : model.speedBlocker)
            }
            GridRow {
                hudNumber("Gap closing", closing.map { String(format: "%.0f", $0 * factor) }, unit, size: size,
                          unavailable: gapMotionBlocker)
                hudNumber("Est. closing time", ttc.map { String(format: "%.1f", $0) }, "s", size: size,
                          alert: (ttc ?? .infinity) < 2,
                          unavailable: risk.leadSpeed != nil && (risk.closingSpeed ?? .infinity) <= 0.5
                            ? "Not closing" : model.collisionBlocker)
            }
        }
        .modifier(CameraPlate())
        .shadow(color: colorScheme == .dark ? .black.opacity(0.9) : .clear, radius: 8, y: 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Car ahead, estimated")
    }

    private func hudNumber(_ label: String, _ value: String?, _ unit: String, size: CGFloat,
                           alert: Bool = false, unavailable: String? = nil) -> some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(label).font(.brake(size: 11, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            Text(value ?? "--").font(.brake(size: size, weight: .semibold)).monospacedDigit()
                .foregroundColor(alert ? BrakeTheme.danger : BrakeTheme.text)
            + Text(" \(unit)").font(.brake(size: 12, weight: .semibold)).foregroundColor(BrakeTheme.muted)
            if value == nil, let unavailable, !unavailable.isEmpty {
                Text(unavailable).font(.brake(size: 10)).foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private var calibrationControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("While parked, draw around the rear of a vehicle and enter its measured gap.").font(.brake(.caption))
            HStack {
                TextField("Meters", text: $measuredDistance).keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                Button("Track") {
                    if let box = selection, let distance = calibration { model.selectVehicle(box: box, measuredDistance: distance); selecting = false }
                }.disabled(selection == nil || !(2...80).contains(calibration ?? 0))
                Button("Cancel") { selecting = false; selection = nil }
            }
        }
        .padding(12)
        .background(BrakeTheme.cameraSurface, in: RoundedRectangle(cornerRadius: 10))
    }
}

/// Light mode has no scrim over the camera, so readouts that would float on the video sit on a plate, the same
/// surface the risk and status pills use. Dark mode leaves them floating over its scrim.
private struct CameraPlate: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    var shape = AnyShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    var padding = EdgeInsets(top: 8, leading: 12, bottom: 8, trailing: 12)

    @ViewBuilder func body(content: Content) -> some View {
        if colorScheme == .light {
            content.padding(padding).background(BrakeTheme.cameraSurface, in: shape)
        } else {
            content
        }
    }
}

/// Clips only the sides, so the camera page can't spill onto its neighbours during a swipe, while its
/// full-bleed layers (camera, placeholder, scrims) still reach up under the hidden status bar.
private struct SideClip: Shape {
    func path(in rect: CGRect) -> Path { Path(rect.insetBy(dx: 0, dy: -rect.height)) }
}
