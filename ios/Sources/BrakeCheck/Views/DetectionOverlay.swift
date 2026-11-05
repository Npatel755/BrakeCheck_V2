import SwiftUI
import BrakeCheckCore

struct LeadCorridorOverlay: View {
    @AppStorage("display.lanePresentation") private var lanePresentation = "corridor"
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    var lane: LanePath?
    var body: some View {
        if let lane, lane.source != .markings {
            GeometryReader { geometry in
                // Only the neural/hybrid guide is shown; paint-only bounds
                // remain internal because they are provisional.
                let bands = lane.bands
                if lanePresentation == "corridor", let first = bands.first {
                    let floor = Path { path in
                        path.move(to: CGPoint(x: first.left * geometry.size.width, y: (1 - first.y) * geometry.size.height))
                        for band in bands.dropFirst() {
                            path.addLine(to: CGPoint(x: band.left * geometry.size.width, y: (1 - band.y) * geometry.size.height))
                        }
                        for band in bands.reversed() {
                            path.addLine(to: CGPoint(x: band.right * geometry.size.width, y: (1 - band.y) * geometry.size.height))
                        }
                        path.closeSubpath()
                    }
                    floor.fill(.cyan.opacity(0.28))
                }
                let path = Path { path in
                    guard let first = bands.first else { return }
                    func point(_ x: Double, _ y: Double) -> CGPoint {
                        CGPoint(x: x * geometry.size.width, y: (1 - y) * geometry.size.height)
                    }
                    path.move(to: point(first.left, first.y))
                    for band in bands.dropFirst() { path.addLine(to: point(band.left, band.y)) }
                    path.move(to: point(first.right, first.y))
                    for band in bands.dropFirst() { path.addLine(to: point(band.right, band.y)) }
                }
                path.stroke(BrakeTheme.accentColor(named: accentChoice).opacity(0.75),
                            style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [8, 5]))
            }
            .allowsHitTesting(false)
        }
    }
}

/// Raw model traces are visual predictions, independent of the accepted
/// two-boundary corridor used internally for safety and road alignment.
struct CameraPerceptionOverlay: View {
    @AppStorage("display.lanePresentation") private var lanePresentation = "corridor"
    let lane: LanePath?
    let predictions: LanePredictionObservation?
    var vehicleBoxes: [CGRect] = []

    private func color(for slot: Int) -> Color {
        switch slot {
        case 0: return .orange
        case 1: return .cyan
        case 2: return .cyan
        default: return .mint
        }
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.1)) { _ in
            let now = ProcessInfo.processInfo.systemUptime
            if let visible = LanePredictionDisplay.visible(predictions, at: now) {
                Canvas { context, size in
                    // Keep the corridor behind currently detected vehicles.
                    for box in vehicleBoxes where box.width > 0 && box.height > 0 {
                        var mask = Path(CGRect(origin: .zero, size: size))
                        mask.addRect(CGRect(x: box.minX * size.width, y: (1 - box.maxY) * size.height,
                                            width: box.width * size.width, height: box.height * size.height))
                        context.clip(to: mask, style: FillStyle(eoFill: true))
                    }
                    if lanePresentation == "corridor" {
                        for section in LaneCorridorDisplay.sections(visible, at: now) {
                            let polygon = Path { path in
                                guard let first = section.polygon.first else { return }
                                path.move(to: CGPoint(x: first.x * size.width, y: (1 - first.y) * size.height))
                                for point in section.polygon.dropFirst() {
                                    path.addLine(to: CGPoint(x: point.x * size.width, y: (1 - point.y) * size.height))
                                }
                                path.closeSubpath()
                            }
                            context.fill(polygon, with: .linearGradient(
                                Gradient(colors: [.cyan.opacity(0.14), .cyan.opacity(0.42)]),
                                startPoint: CGPoint(x: size.width / 2, y: 0),
                                endPoint: CGPoint(x: size.width / 2, y: size.height)))
                        }
                    }
                    for boundary in visible.boundaries {
                        let path = Path { path in
                            guard let first = boundary.points.first else { return }
                            func point(_ point: LanePredictionObservation.Point) -> CGPoint {
                                CGPoint(x: point.x * size.width, y: (1 - point.y) * size.height)
                            }
                            path.move(to: point(first))
                            for value in boundary.points.dropFirst() { path.addLine(to: point(value)) }
                        }
                        let edgeColor: Color = lanePresentation == "corridor" && boundary.laneSlot == 1
                            ? .yellow : lanePresentation == "corridor" && boundary.laneSlot == 2
                            ? .white : color(for: boundary.laneSlot)
                        let inner = boundary.laneSlot == 1 || boundary.laneSlot == 2
                        context.stroke(path, with: .color(edgeColor.opacity(0.9)),
                            style: StrokeStyle(lineWidth: lanePresentation == "corridor" && inner ? 3 : 1.8,
                                               lineCap: .round, lineJoin: .round))
                    }
                }
                .accessibilityLabel("Model-predicted lane boundaries")
            } else if let lane, lane.isUsable(at: now) {
                // A fresh confirmed corridor can be a fallback, without
                // drawing a duplicate pair or holding a stale guide.
                LeadCorridorOverlay(lane: lane)
            }
        }
        .allowsHitTesting(false)
    }
}

/// Compact model status is screen anchored, never stretched with the camera.
struct CameraLanePredictionStatus: View {
    @AppStorage("display.lanePresentation") private var lanePresentation = "corridor"
    @AppStorage("display.laneTrackingMode") private var trackingMode = "kalman"
    let model: DriveModel

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.1)) { _ in
            let now = ProcessInfo.processInfo.systemUptime
            let corridor = !LaneCorridorDisplay.sections(model.lanePredictions, at: now).isEmpty
            let prefix = lanePresentation == "corridor" ? (corridor ? "CORRIDOR" : "CORRIDOR · waiting for both sides") : "LINES"
            Text("\(prefix) · \(LaneTrackingMode(rawValue: trackingMode)?.title ?? "Kalman") · \(model.laneGuideStatus(at: now))")
                .font(.brake(size: 10, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))
                .padding(.horizontal, 8).padding(.vertical, 5)
                .background(.black.opacity(0.62), in: RoundedRectangle(cornerRadius: 8))
                .accessibilityLabel("Lane model status")
        }
        .allowsHitTesting(false)
    }
}

/// Road-model diagnostic for the Intel road mask. Draws raw pixels
/// even when the alignment scanner declines to use them for navigation.
struct CameraRoadMaskOverlay: View {
    let presentation: CameraARPresentation

    var body: some View {
        GeometryReader { _ in
            Canvas { context, size in
                let path = presentation.diagnosticRoadPath().applying(
                    CGAffineTransform(scaleX: size.width, y: size.height))
                context.fill(path, with: .color(.cyan.opacity(0.28)))
            }
        }
        .allowsHitTesting(false)
    }
}

/// Screen-anchored status, outside the aspect-filled camera content.
struct CameraRoadMaskStatus: View {
    let presentation: CameraARPresentation
    let portrait: Bool

    private var status: String {
        guard let scene = presentation.scene, let mask = scene.diagnosticRoadMask else {
            return "ROAD MASK · waiting"
        }
        let count = mask.occupiedPixelCount
        let percent = Int((Double(count) / Double(mask.pixels.count) * 100).rounded())
        return scene.road == nil ? "ROAD MASK · \(percent)% · alignment rejected"
            : "ROAD MASK · \(percent)%"
    }

    var body: some View {
        VStack {
            Text(status)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white)
                .padding(.horizontal, 8).padding(.vertical, 5)
                .background(.black.opacity(0.72), in: Capsule())
                .frame(maxWidth: .infinity, alignment: .leading)
            Spacer()
        }
        .padding(.horizontal, 56)
        .padding(.top, portrait ? 88 : 49)
        .allowsHitTesting(false)
    }
}

struct DetectionOverlay: View {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    let detections: [VehicleDetection]
    var hazardTargetID: UUID? = nil
    var leadTargetID: UUID? = nil
    private func boxColor(_ item: VehicleDetection) -> Color {
        if item.id == hazardTargetID { return BrakeTheme.warning }
        if item.id == leadTargetID { return BrakeTheme.accent }
        return BrakeTheme.accent.opacity(0.65)
    }
    var body: some View {
        GeometryReader { geometry in
            ForEach(detections.filter { $0.isFreshDetection }) { item in
                // Keep the road clear: fresh boxes show detection state without
                // confidence scores, track IDs or viewpoint diagnostics.
                Rectangle().stroke(boxColor(item).opacity(0.7), lineWidth: 1)
                .frame(width: item.box.width * geometry.size.width, height: item.box.height * geometry.size.height)
                .position(x: item.box.midX * geometry.size.width, y: (1 - item.box.midY) * geometry.size.height)
            }
        }
        .allowsHitTesting(false)
        .tint(BrakeTheme.accentColor(named: accentChoice))
    }
}
