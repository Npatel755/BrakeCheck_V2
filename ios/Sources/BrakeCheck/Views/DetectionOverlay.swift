import SwiftUI
import BrakeCheckCore

struct RoadAreaOverlay: View {
    @AppStorage("display.accent") private var accentChoice = "blue"
    var road: RoadRegion?
    var body: some View {
        GeometryReader { geometry in
            let path = Path { path in
                guard let bands = road?.bands, let first = bands.first else { return }
                func point(_ x: Double, _ y: Double) -> CGPoint {
                    CGPoint(x: x * geometry.size.width, y: (1 - y) * geometry.size.height)
                }
                path.move(to: point(first.left, first.y))
                for band in bands.dropFirst() { path.addLine(to: point(band.left, band.y)) }
                for band in bands.reversed() { path.addLine(to: point(band.right, band.y)) }
                path.closeSubpath()
            }
            path.fill(BrakeTheme.accentColor(named: accentChoice).opacity(0.055))
            path.stroke(BrakeTheme.accentColor(named: accentChoice).opacity(0.60), style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
        }.allowsHitTesting(false)
    }
}

struct LeadCorridorOverlay: View {
    @AppStorage("display.accent") private var accentChoice = "blue"
    var lane: LanePath?
    var body: some View {
        GeometryReader { geometry in
            // Draw measured geometry only. An absent lane must not leave a
            // centered trapezoid that looks like a restricted detector view.
            let bands = lane?.bands ?? []
            let path = Path { path in
                guard let first = bands.first else { return }
                func point(_ x: Double, _ y: Double) -> CGPoint {
                    CGPoint(x: x * geometry.size.width, y: (1 - y) * geometry.size.height)
                }
                path.move(to: point(first.left, first.y))
                for band in bands.dropFirst() { path.addLine(to: point(band.left, band.y)) }
                for band in bands.reversed() { path.addLine(to: point(band.right, band.y)) }
                path.closeSubpath()
            }
            path.fill(BrakeTheme.accentColor(named: accentChoice).opacity(0.07))
            path.stroke(BrakeTheme.accentColor(named: accentChoice).opacity(0.75), lineWidth: 2)
        }
        .allowsHitTesting(false)
    }
}

struct DetectionOverlay: View {
    @AppStorage("display.accent") private var accentChoice = "blue"
    let detections: [VehicleDetection]
    var hazardTargetID: UUID? = nil
    var leadTargetID: UUID? = nil
    private func boxColor(_ item: VehicleDetection) -> Color {
        if !item.isFreshDetection { return BrakeTheme.caution }
        if item.id == hazardTargetID { return BrakeTheme.warning }
        if item.id == leadTargetID { return BrakeTheme.accent }
        return BrakeTheme.accent.opacity(0.65)
    }
    var body: some View {
        GeometryReader { geometry in
            ForEach(detections) { item in
                // Keep the road clear: boxes show detection state without
                // confidence scores, track IDs or viewpoint diagnostics.
                Rectangle().stroke(boxColor(item).opacity(item.isFreshDetection ? 0.7 : 0.4),
                        style: StrokeStyle(lineWidth: 1, dash: item.isFreshDetection ? [] : [4, 3]))
                .frame(width: item.box.width * geometry.size.width, height: item.box.height * geometry.size.height)
                .position(x: item.box.midX * geometry.size.width, y: (1 - item.box.midY) * geometry.size.height)
            }
        }
        .allowsHitTesting(false)
        .tint(BrakeTheme.accentColor(named: accentChoice))
    }
}
