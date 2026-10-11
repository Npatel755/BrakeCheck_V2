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
    var hazardTargetID: UUID? = nil
    private func direction(_ item: VehicleDetection) -> (caption: String, color: Color) {
        if item.id == hazardTargetID { return ("CLOSING HAZARD", .orange) }
        guard ["car", "truck"].contains(item.label) else { return ("VIEW UNSUPPORTED", .white) }
        guard let view = item.viewpoint, view.isFresh(at: item.capturedAt) else {
            return ("CHECKING VIEW", .yellow)
        }
        if view.isFrontFacing(at: item.capturedAt) { return ("FRONT VIEW · NOT A LEAD", .orange) }
        if view.hasConfirmedRearView(at: item.capturedAt) { return ("REAR · CONFIRMED", BrakeTheme.accent) }
        if view.isRearFacing(at: item.capturedAt) { return ("REAR · CHECKING AGAIN", .yellow) }
        return (String(format: "UNCERTAIN · F %.0f%% R %.0f%%", view.front * 100, view.rear * 100), .yellow)
    }
    var body: some View {
        GeometryReader { geometry in
            ForEach(detections) { item in
                let view = direction(item)
                ZStack(alignment: .topLeading) {
                    Rectangle().stroke(view.color.opacity(0.7), lineWidth: 1)
                    Text("\(item.label) \(item.confidence, specifier: "%.2f")\n\(view.caption)")
                        .font(.system(size: 10, weight: .bold)).foregroundStyle(view.color)
                        .lineLimit(2).minimumScaleFactor(0.6)
                        .padding(3).background(.black.opacity(0.6))
                }
                .frame(width: item.box.width * geometry.size.width, height: item.box.height * geometry.size.height)
                .position(x: item.box.midX * geometry.size.width, y: (1 - item.box.midY) * geometry.size.height)
            }
        }
        .allowsHitTesting(false)
    }
}
