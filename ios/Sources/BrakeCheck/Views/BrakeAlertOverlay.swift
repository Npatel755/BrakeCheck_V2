import BrakeCheckCore
import SwiftUI

/// The collision-risk pill, top center: the main focus of the dashboard. It grows a little at high risk and more
/// at critical, where it turns red at the same moment as the "Brake" voice and haptic.
/// A recent alert remains visible without describing a recovered target as critical.
struct RiskPill: View {
    @AppStorage("display.accent") private var accentChoice = "blue"
    let risk: RiskSnapshot
    let critical: Bool
    var meter: DangerMeterSnapshot? = nil
    /// Larger base size for the vertical layout.
    var large = false
    var unavailableTitle: String? = nil

    private var meterLevel: RiskLevel { meter?.level ?? risk.level }
    private var meterScore: Double? {
        if let meter { return meter.score }
        return risk.score
    }
    private var scale: CGFloat { critical ? 1.3 : meterLevel == .high ? 1.12 : 1 }
    private var currentCritical: Bool { risk.level == .critical }
    private var title: String {
        if critical { return currentCritical ? "BRAKE NOW" : "BRAKE ALERT" }
        if meterScore == nil, let unavailableTitle { return unavailableTitle }
        return BrakeTheme.name(for: meterLevel)
    }

    var body: some View {
        HStack(spacing: large ? 12 : 10) {
            if critical {
                Image(systemName: "exclamationmark.triangle.fill")
            } else {
                Circle().fill(BrakeTheme.color(for: meterLevel)).frame(width: 7, height: 7)
            }
            Text(title).tracking(critical ? 1.2 : 0).monospacedDigit().lineLimit(1)
                .minimumScaleFactor(0.7)
                .foregroundStyle(critical ? Color.white : BrakeTheme.color(for: meterLevel))
            // A latched alert must not light every step after current danger has fallen.
            if !(critical && currentCritical && large) {
                DangerSteps(score: meterScore, onAlert: critical && currentCritical, large: large)
            }
        }
        .font(.brake(size: large ? 17 : 14, weight: critical ? .heavy : .bold))
        .foregroundStyle(.white)
        .padding(.horizontal, large ? 18 : 14)
        .frame(height: large ? 44 : 34)
        .background(critical ? BrakeTheme.danger : BrakeTheme.cameraSurface, in: Capsule())
        .shadow(color: critical ? BrakeTheme.danger.opacity(0.7) : .clear, radius: 16)
        .scaleEffect(scale)
        .animation(.spring(response: 0.3, dampingFraction: 0.6), value: scale)
        .allowsHitTesting(false)
        .tint(BrakeTheme.accentColor(named: accentChoice))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(critical
            ? currentCritical ? "Brake warning. Brake now."
                : "Recent brake warning. Current collision risk: \(BrakeTheme.name(for: meterLevel))."
            : "Collision risk: \(title)")
    }
}

/// Four small steps that light up in order, blue to red. Step edges match the score cut-offs in RiskRules.evaluate.
struct DangerSteps: View {
    @AppStorage("display.accent") private var accentChoice = "blue"
    let score: Double?
    /// On the red BRAKE background the steps are drawn in white.
    var onAlert = false
    var large = false

    private static let steps: [(level: RiskLevel, start: Double)] = [(.low, 0), (.elevated, 30), (.high, 60), (.critical, 85)]

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<Self.steps.count, id: \.self) { index in
                let step = Self.steps[index]
                let lit = onAlert || (score.map { $0 >= step.start } ?? false)
                RoundedRectangle(cornerRadius: 2)
                    .fill(onAlert ? Color.white : BrakeTheme.color(for: step.level))
                    .opacity(lit ? 1 : (onAlert ? 0.35 : 0.18))
                    .frame(width: large ? 20 : 16, height: large ? 6 : 5)
            }
        }
        .accessibilityHidden(true)
        .tint(BrakeTheme.accentColor(named: accentChoice))
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
