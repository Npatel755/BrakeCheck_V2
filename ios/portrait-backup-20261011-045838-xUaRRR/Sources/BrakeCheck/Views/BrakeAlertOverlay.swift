import BrakeCheckCore
import SwiftUI

/// The collision-risk pill, top center: the main focus of the dashboard. It grows a little at high risk and more
/// at critical, where it turns red and reads BRAKE, at the same moment as the "Brake" voice and haptic.
struct RiskPill: View {
    let risk: RiskSnapshot
    let critical: Bool
    /// True while the camera runs but no lead vehicle is being tracked.
    let searching: Bool
    /// The box is visible, but fresh detection or direction evidence is missing.
    var paused = false
    var selectionStatus = "Looking for a car ahead"
    /// Larger base size for the vertical layout.
    var large = false

    private var scale: CGFloat { critical ? 1.3 : risk.level == .high ? 1.12 : 1 }
    private var title: String {
        if critical { return risk.timeToCollision.map { String(format: "BRAKE · %.1f s", $0) } ?? "BRAKE" }
        if paused || searching { return selectionStatus }
        return BrakeTheme.name(for: risk.level)
    }

    var body: some View {
        HStack(spacing: large ? 12 : 10) {
            if critical {
                Image(systemName: "exclamationmark.triangle.fill")
            } else {
                Circle().fill(BrakeTheme.color(for: risk.level)).frame(width: 7, height: 7)
            }
            Text(title).tracking(critical ? 1.2 : 0).monospacedDigit().lineLimit(1)
                .minimumScaleFactor(0.7)
                .foregroundStyle(critical ? Color.white : BrakeTheme.color(for: risk.level))
            // In the vertical layout the grown BRAKE pill would overflow the screen; every step is lit then anyway.
            if !(critical && large) {
                DangerSteps(score: risk.score, onAlert: critical, large: large)
            }
        }
        .font(.system(size: large ? 17 : 14, weight: critical ? .heavy : .bold))
        .foregroundStyle(.white)
        .padding(.horizontal, large ? 18 : 14)
        .frame(height: large ? 44 : 34)
        .background(critical ? BrakeTheme.danger : Color.black.opacity(0.5), in: Capsule())
        .shadow(color: critical ? BrakeTheme.danger.opacity(0.7) : .clear, radius: 16)
        .scaleEffect(scale)
        .animation(.spring(response: 0.3, dampingFraction: 0.6), value: scale)
        .allowsHitTesting(false)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(critical ? "Brake warning. \(title)" : "Collision risk: \(title)")
    }
}

/// Four small steps that light up in order, green to red. Step edges match the score cut-offs in RiskRules.evaluate.
struct DangerSteps: View {
    let score: Double?
    /// On the red BRAKE background the steps are drawn in white.
    var onAlert = false
    var large = false

    private static let steps: [(level: RiskLevel, start: Double)] = [(.low, 0), (.elevated, 30), (.high, 60), (.critical, 85)]

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<Self.steps.count, id: \.self) { index in
                let step = Self.steps[index]
                let lit = score.map { $0 >= step.start } ?? false
                RoundedRectangle(cornerRadius: 2)
                    .fill(onAlert ? Color.white : BrakeTheme.color(for: step.level))
                    .opacity(lit ? 1 : (onAlert ? 0.35 : 0.18))
                    .frame(width: large ? 20 : 16, height: large ? 6 : 5)
            }
        }
        .accessibilityHidden(true)
    }
}

/// A pulsing red edge around the whole screen, readable from the passenger seat or a judging table.
struct BrakeEdgeGlow: View {
    @State private var pulse = false

    var body: some View {
        Rectangle()
            .strokeBorder(BrakeTheme.danger, lineWidth: 14)
            .blur(radius: 8)
            .opacity(pulse ? 0.45 : 1)
            .ignoresSafeArea()
            .allowsHitTesting(false)
            .accessibilityHidden(true)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.45).repeatForever(autoreverses: true)) { pulse = true }
            }
    }
}
