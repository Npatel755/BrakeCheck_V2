import SwiftUI

/// Shown once before first use: what the warnings can and can't do. Driving apps get extra App
/// Review scrutiny, and the limits belong in front of the driver, not only in Settings > About.
struct DriverAidNoticeView: View {
    var accept: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                BrakeLogo(size: 30)
                Text("A driver aid, not a safety system.")
                    .font(.brake(size: 28, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
                point("eye", "Keep your eyes on the road. BrakeSense's warnings can be late, missed, or wrong.")
                point("iphone.gen3", "Mount the phone and set it up before you drive. Don't touch it while moving.")
                point("car.rear.road.lane", "Distances and speeds are estimates from the camera and GPS, and are less reliable at night or in rain.")
                Button(action: accept) {
                    Text("I understand")
                        .font(.brake(size: 16, weight: .bold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 15)
                        .foregroundStyle(BrakeTheme.background)
                        .background(BrakeTheme.accent, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("notice.accept")
                .padding(.top, 8)
            }
            .padding(28)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .background(BrakeTheme.background.ignoresSafeArea())
        .foregroundStyle(BrakeTheme.text)
        .interactiveDismissDisabled()
    }

    private func point(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon)
                .font(.brake(size: 20))
                .foregroundStyle(BrakeTheme.accentText)
                .frame(width: 28)
                .accessibilityHidden(true)
            Text(text).font(.brake(.subheadline)).fixedSize(horizontal: false, vertical: true)
        }
    }
}
