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
                    Text("BrakeCheck Premium adds deeper trip insights and camera display controls.")
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
                if store.isSubscribed {
                    Toggle("Use compact camera directions", isOn: $compactBanner)
                        .font(.brake(.subheadline, weight: .semibold))
                        .padding(18)
                        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18))
                }

                Text("ON THE ROADMAP")
                    .font(.brake(size: 12, weight: .bold))
                    .tracking(1.2)
                    .foregroundStyle(BrakeTheme.muted)
                benefit("Enhanced AR overlays", detail: "Additional camera-aligned styles are planned. Current AR guidance remains available to everyone.", icon: "viewfinder")
                benefit("Cloud-assisted model", detail: "A stronger optional cloud model is being researched and is not available yet.", icon: "cloud")
                Text("Roadmap items are not included in today's purchase. Safety alerts, basic trip summaries, and current AR guidance stay free.")
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
                    Text("Premium purchases are unavailable on this build.")
                        .font(.brake(.subheadline, weight: .semibold))
                    Text("Planned US price: $9.99/month. Purchases become available after the subscription is configured in App Store Connect.")
                        .font(.brake(.caption))
                        .foregroundStyle(BrakeTheme.muted)
                }
                Button("Restore purchases") { Task { await store.restore() } }
                    .disabled(store.isBusy)
                if let message = store.message {
                    Text(message).font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                }
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
