import StoreKit
import SwiftUI

struct PremiumView: View {
    @ObservedObject private var store = PremiumStore.shared
    @AppStorage("display.premiumCompactBanner") private var compactBanner = false
    var onContinue: (() -> Void)? = nil

    init(onContinue: (() -> Void)? = nil) {
        self.onContinue = onContinue
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 14) {
                    Image(systemName: "sparkles.rectangle.stack.fill")
                        .font(.brake(size: 42, weight: .semibold))
                    Text(onContinue == nil ? "Drive with more insight" : "Your first drive is saved")
                        .font(.brake(size: 34, weight: .bold))
                    Text("BrakeSense Premium adds optional driving alerts, deeper trip insights, and camera display controls.")
                        .font(.brake(.title3))
                }
                .foregroundStyle(BrakeTheme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(24)
                .background(
                    LinearGradient(colors: [BrakeTheme.accent.opacity(0.28), BrakeTheme.panel],
                                   startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: RoundedRectangle(cornerRadius: 28))

                Text("AVAILABLE WITH PREMIUM")
                    .font(.brake(size: 12, weight: .bold))
                    .tracking(1.2)
                    .foregroundStyle(BrakeTheme.muted)
                benefit("Deeper trip insights", detail: "See approximate route distance and average speed for saved trips.", icon: "chart.xyaxis.line")
                benefit("Compact camera directions", detail: "Choose a smaller turn banner to reveal more of the road.", icon: "rectangle.compress.vertical")
                DrivingAssistControls(offerUpgrade: false)
                    .padding(18)
                    .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
                if store.isSubscribed {
                    Toggle("Use compact camera directions", isOn: $compactBanner)
                        .font(.brake(.subheadline, weight: .semibold))
                        .padding(18)
                        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18))
                }

                Text("Collision warnings, emergency check-ins, basic trip summaries, and camera directions stay free.")
                    .font(.brake(.caption))
                    .foregroundStyle(BrakeTheme.muted)
                if store.isSubscribed {
                    Label("Premium active", systemImage: "checkmark.seal.fill")
                        .foregroundStyle(BrakeTheme.accentText)
                } else if let product = store.product {
                    Text("\(product.displayPrice) per month. Auto-renews until cancelled in Apple Account settings.")
                        .font(.brake(.subheadline))
                    Button {
                        Task { await store.purchase() }
                    } label: {
                        Text(store.isBusy ? "Connecting to App Store…" : "Subscribe to Premium")
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.isBusy)
                } else if store.isLoadingProduct {
                    ProgressView("Checking App Store availability")
                } else {
                    // The App Store sets the price; until the product loads, never show one.
                    Text("Premium can't load from the App Store right now.")
                        .font(.brake(.subheadline, weight: .semibold))
                    Button("Try again") { Task { await store.load() } }
                        .font(.brake(.subheadline, weight: .semibold))
                }
                Button("Restore purchases") { Task { await store.restore() } }
                    .disabled(store.isBusy)
                if let message = store.message {
                    Text(message).font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                }
                HStack(spacing: 18) {
                    Link("Terms of Use", destination: LegalLinks.termsOfUse)
                    if let privacy = LegalLinks.privacyPolicy {
                        Link("Privacy Policy", destination: privacy)
                    }
                }
                .font(.brake(.caption, weight: .semibold))
                .foregroundStyle(BrakeTheme.accentText)
            }
            .frame(maxWidth: 620, alignment: .leading)
            .padding(24)
            .frame(maxWidth: .infinity)
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .accessibilityIdentifier("premiumPreview")
        .task { await store.load() }
        .safeAreaInset(edge: .bottom) {
            if let onContinue {
                Button(action: onContinue) {
                    Text("See my trip summary")
                        .font(.brake(.headline))
                        .frame(maxWidth: .infinity, minHeight: 52)
                }
                .buttonStyle(.borderedProminent)
                .padding(.horizontal, 20)
                .padding(.vertical, 10)
                .background(BrakeTheme.background)
            }
        }
    }

    private func benefit(_ title: String, detail: String, icon: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon)
                .font(.brake(size: 22, weight: .semibold))
                .foregroundStyle(BrakeTheme.accentText)
                .frame(width: 34)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.brake(.headline))
                Text(detail).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
    }
}
