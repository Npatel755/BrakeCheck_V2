import SwiftUI
import BrakeCheckCore

struct LeadCorridorOverlay: View {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    var lane: LanePath?
    var body: some View {
        if let lane, lane.source != .markings {
            GeometryReader { geometry in
                // Only the neural/hybrid guide is shown; paint-only bounds
                // remain internal because they are provisional.
                let bands = lane.bands
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

/// Optional diagnostic guides stay out of the way of an aligned AR ribbon.
struct CameraPerceptionOverlay: View {
    @ObservedObject var presentation: CameraARPresentation
    let lane: LanePath?
    let navigationOnCamera: Bool
    var body: some View {
        // Diagnostic lane boundaries stay visible during the road-model test,
        // including when the AR ribbon is aligned.
        LeadCorridorOverlay(lane: lane)
    }
}

/// Temporary road-model diagnostic for the Intel test build. Draws raw pixels
/// even when the alignment scanner declines to use them for navigation.
struct CameraRoadMaskOverlay: View {
    @ObservedObject var presentation: CameraARPresentation

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
    @ObservedObject var presentation: CameraARPresentation
    let portrait: Bool

    private var status: String {
        guard let scene = presentation.scene, let mask = scene.diagnosticRoadMask else {
            return "ROAD MASK · waiting"
        }
        let count = mask.pixels.reduce(0) { $0 + ($1 == 0 ? 0 : 1) }
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
