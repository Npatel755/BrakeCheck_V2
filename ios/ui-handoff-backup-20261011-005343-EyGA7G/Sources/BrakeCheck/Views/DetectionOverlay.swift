import SwiftUI
import BrakeCheckCore

struct RoadAreaOverlay: View {
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
            path.fill(Color.orange.opacity(0.055))
            path.stroke(Color.orange.opacity(0.60), style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
        }.allowsHitTesting(false)
    }
}

struct LeadCorridorOverlay: View {
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
            path.fill(BrakeTheme.accent.opacity(0.07))
            path.stroke(BrakeTheme.accent.opacity(0.75), lineWidth: 2)
        }
        .allowsHitTesting(false)
    }
}

struct DetectionOverlay: View {
    let detections: [VehicleDetection]
    var body: some View {
        GeometryReader { geometry in
            ForEach(detections) { item in
                ZStack(alignment: .topLeading) {
                    Rectangle().stroke(Color.white.opacity(0.4), lineWidth: 1)
                    Text("\(item.label) \(item.confidence, specifier: "%.2f")")
                        .font(.system(size: 9, weight: .bold)).foregroundStyle(.white.opacity(0.8))
                        .padding(3).background(.black.opacity(0.6))
                }
                .frame(width: item.box.width * geometry.size.width, height: item.box.height * geometry.size.height)
                .position(x: item.box.midX * geometry.size.width, y: (1 - item.box.midY) * geometry.size.height)
            }
        }
        .allowsHitTesting(false)
    }
}
