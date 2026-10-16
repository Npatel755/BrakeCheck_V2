import SwiftUI

/// Brake lamp and wordmark in the shared blue, with text that follows appearance.
struct BrakeLogo: View {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    var size: CGFloat = 25

    var body: some View {
        HStack(spacing: size * 0.3) {
            BrakeLamp().frame(width: size * 1.2, height: size * 1.2)
            (Text("Brake").foregroundColor(BrakeTheme.text) + Text("Check").foregroundColor(BrakeTheme.accentText))
                .font(.brake(size: size, weight: .bold)).tracking(-size * 0.025)
                .lineLimit(1).fixedSize()
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("BrakeCheck")
        .tint(BrakeTheme.accentColor(named: accentChoice))
    }
}

/// The ISO brake warning lamp: a ring with an exclamation mark and an arc on each side.
/// Drawn on a 28-unit grid so it stays sharp at any size.
struct BrakeLamp: View {
    var color: Color = BrakeTheme.accentText
    var lineWidth: CGFloat = 2.6

    var body: some View {
        Canvas { context, size in
            let s = min(size.width, size.height) / 28
            context.translateBy(x: (size.width - 28 * s) / 2, y: (size.height - 28 * s) / 2)
            context.scaleBy(x: s, y: s)
            let style = StrokeStyle(lineWidth: lineWidth, lineCap: .round)

            context.stroke(Path(ellipseIn: CGRect(x: 6.5, y: 6.5, width: 15, height: 15)), with: .color(color), style: style)

            var stem = Path()
            stem.move(to: CGPoint(x: 14, y: 9.8))
            stem.addLine(to: CGPoint(x: 14, y: 15))
            context.stroke(stem, with: .color(color), style: StrokeStyle(lineWidth: lineWidth + 0.2, lineCap: .round))
            context.fill(Path(ellipseIn: CGRect(x: 12.4, y: 16.6, width: 3.2, height: 3.2)), with: .color(color))

            // Side arcs, as quadratic curves through the arc's midpoint (radius about 11.4 around the centre).
            var arcs = Path()
            arcs.move(to: CGPoint(x: 4.6, y: 7.5))
            arcs.addQuadCurve(to: CGPoint(x: 4.6, y: 20.5), control: CGPoint(x: 0.6, y: 14))
            arcs.move(to: CGPoint(x: 23.4, y: 7.5))
            arcs.addQuadCurve(to: CGPoint(x: 23.4, y: 20.5), control: CGPoint(x: 27.4, y: 14))
            context.stroke(arcs, with: .color(color), style: style)
        }
        .accessibilityHidden(true)
    }
}
