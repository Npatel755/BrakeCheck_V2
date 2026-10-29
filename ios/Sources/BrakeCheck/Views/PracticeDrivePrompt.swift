import SwiftUI

/// Onboarding, right after the driver-aid notice: offer a short simulated drive so the first
/// thing a new driver sees is a warning working. The Premium offer only follows if they watch it.
struct PracticeDrivePrompt: View {
    var start: () -> Void
    var skip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: "car.side.hill.up.fill")
                .font(.brake(size: 30, weight: .semibold))
                .foregroundStyle(BrakeTheme.accentText)
                .accessibilityHidden(true)
            Text("See a warning before you drive")
                .font(.brake(size: 26, weight: .bold))
                .fixedSize(horizontal: false, vertical: true)
            Text("Watch a 20-second practice drive. The car ahead brakes hard and BrakeSense warns you. It's simulated: the camera stays off and nothing is recorded.")
                .font(.brake(.subheadline))
                .foregroundStyle(BrakeTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Button(action: start) {
                Text("Start practice drive")
                    .font(.brake(size: 16, weight: .bold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 15)
                    .foregroundStyle(BrakeTheme.background)
                    .background(BrakeTheme.accent, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("practice.start")
            Button(action: skip) {
                Text("Skip for now")
                    .font(.brake(.subheadline, weight: .semibold))
                    .foregroundStyle(BrakeTheme.muted)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("practice.skip")
        }
        .padding(26)
        .frame(maxWidth: 560, maxHeight: .infinity, alignment: .top)
        .frame(maxWidth: .infinity)
        .background(BrakeTheme.background.ignoresSafeArea())
        .foregroundStyle(BrakeTheme.text)
    }
}

/// Shown over the camera page while the practice drive runs.
struct PracticeDriveBanner: View {
    var end: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Label("Practice drive · simulated", systemImage: "play.circle.fill")
                .font(.brake(.subheadline, weight: .semibold))
            Spacer(minLength: 8)
            Button("End", action: end)
                .font(.brake(.subheadline, weight: .bold))
                .accessibilityIdentifier("practice.end")
        }
        .foregroundStyle(BrakeTheme.text)
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(BrakeTheme.cameraSurface, in: Capsule())
        .padding(.horizontal, 16)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("practice.banner")
    }
}
