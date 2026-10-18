import BrakeCheckCore
import StoreKit
import SwiftUI

/// What Plus adds, shown on the paywall. List only features that ship in this build:
/// advertising unbuilt features gets apps rejected. Add rows here as Plus features land.
enum PlusFeatures {
    struct Row: Identifiable {
        let icon: String
        let text: String
        var id: String { text }
    }

    static let rows = [
        Row(icon: "clock.arrow.circlepath", text: "Your full trip history. Free keeps your last \(PlusPolicy.freeTripHistoryCount) trips."),
        Row(icon: "heart", text: "Support two student developers keeping BrakeCheck free for safety")
    ]
}

/// The Plus offer: annual above monthly, a trial timeline when there's a trial, one primary
/// button, an obvious close button, and the price and renewal terms beside the button.
struct PlusPaywallView: View {
    @EnvironmentObject private var store: PlusStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedID = PlusStore.annualID

    private var selected: Product? { store.product(selectedID) ?? store.products.first }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    if store.isPlus {
                        ownedPanel
                    } else if store.loading && store.products.isEmpty {
                        ProgressView("Loading plans…").frame(maxWidth: .infinity).padding(.vertical, 30)
                    } else if store.products.isEmpty {
                        unavailablePanel
                    } else {
                        features
                        plans
                        if selected?.id == PlusStore.annualID, store.annualTrialEligible, let selected { trialTimeline(selected) }
                        purchaseButton
                        terms
                    }
                    footerLinks
                }
                .padding(24)
                .frame(maxWidth: 560)
                .frame(maxWidth: .infinity)
            }
            .background(BrakeTheme.background.ignoresSafeArea())
            .foregroundStyle(BrakeTheme.text)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.brake(size: 26))
                            .foregroundStyle(BrakeTheme.muted)
                    }
                    .accessibilityLabel("Close")
                }
            }
            .alert("BrakeCheck Plus", isPresented: Binding(get: { store.message != nil }, set: { if !$0 { store.message = nil } })) {
                Button("OK", role: .cancel) { store.message = nil }
            } message: { Text(store.message ?? "") }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            BrakeLogo(size: 30)
            Text("BrakeCheck Plus").font(.brake(size: 30, weight: .bold))
            Text("Every safety warning stays free. Plus adds extras for people who drive with BrakeCheck every day.")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var features: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(PlusFeatures.rows) { row in
                HStack(alignment: .top, spacing: 14) {
                    Image(systemName: row.icon).font(.brake(size: 18)).foregroundStyle(BrakeTheme.accent)
                        .frame(width: 26).accessibilityHidden(true)
                    Text(row.text).font(.brake(.subheadline)).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .brakePanel()
    }

    private var plans: some View {
        VStack(spacing: 10) {
            ForEach(store.products, id: \.id) { product in
                Button { selectedID = product.id } label: { planRow(product) }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(selected?.id == product.id ? .isSelected : [])
            }
        }
    }

    private func planRow(_ product: Product) -> some View {
        let isSelected = selected?.id == product.id
        return HStack(spacing: 14) {
            Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                .font(.brake(size: 22)).foregroundStyle(isSelected ? BrakeTheme.accent : BrakeTheme.muted)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(planName(product)).font(.brake(.headline))
                    if let badge = badge(product) {
                        Text(badge).font(.brake(size: 11, weight: .bold))
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .foregroundStyle(BrakeTheme.background)
                            .background(BrakeTheme.accent, in: Capsule())
                    }
                }
                Text(planDetail(product)).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            Spacer()
            Text(product.displayPrice).font(.brake(.headline)).monospacedDigit()
        }
        .padding(16)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(isSelected ? BrakeTheme.accent : .clear, lineWidth: 2))
    }

    private func planName(_ product: Product) -> String {
        switch product.id {
        case PlusStore.annualID: return "Yearly"
        case PlusStore.monthlyID: return "Monthly"
        default: return "Lifetime"
        }
    }

    private func planDetail(_ product: Product) -> String {
        switch product.id {
        case PlusStore.annualID:
            let perMonth = (product.price / 12).formatted(product.priceFormatStyle)
            let trial = store.annualTrialEligible ? trialLength(product).map { "\($0) free, then " } ?? "" : ""
            return "\(trial)\(product.displayPrice) a year (\(perMonth) a month)"
        case PlusStore.monthlyID: return "\(product.displayPrice) a month"
        default: return "Pay once. No subscription."
        }
    }

    private func badge(_ product: Product) -> String? {
        guard product.id == PlusStore.annualID, let monthly = store.product(PlusStore.monthlyID),
              let percent = PlusPolicy.annualSavingsPercent(monthly: monthly.price, annual: product.price) else { return nil }
        return "SAVE \(percent)%"
    }

    private func trialLength(_ product: Product) -> String? {
        guard let offer = product.subscription?.introductoryOffer, offer.paymentMode == .freeTrial,
              let days = PlusStore.days(in: offer.period) else { return nil }
        return "\(days)-day trial"
    }

    private func trialTimeline(_ product: Product) -> some View {
        let days = product.subscription?.introductoryOffer.flatMap { PlusStore.days(in: $0.period) } ?? 7
        return VStack(alignment: .leading, spacing: 12) {
            timelineStep("lock.open", "Today", "Full access to Plus. No charge.")
            if let reminder = PlusPolicy.trialReminderDay(trialDays: days) {
                timelineStep("bell", "Day \(reminder)", "We send a reminder if notifications are on.")
            }
            timelineStep("creditcard", "Day \(days)", "You're charged \(product.displayPrice) for the year unless you cancel before then.")
        }
        .brakePanel()
    }

    private func timelineStep(_ icon: String, _ title: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon).foregroundStyle(BrakeTheme.accent).frame(width: 24).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.brake(.subheadline, weight: .semibold))
                Text(text).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var purchaseButton: some View {
        Button {
            guard let selected else { return }
            Task { if await store.buy(selected) { dismiss() } }
        } label: {
            Group {
                if store.purchasing { ProgressView().tint(BrakeTheme.background) } else { Text(buttonTitle) }
            }
            .font(.brake(size: 17, weight: .bold))
            .frame(maxWidth: .infinity).padding(.vertical, 16)
            .foregroundStyle(BrakeTheme.background)
            .background(BrakeTheme.accent, in: Capsule())
        }
        .buttonStyle(.plain)
        .disabled(store.purchasing || selected == nil)
    }

    private var buttonTitle: String {
        guard let selected else { return "Continue" }
        if selected.id == PlusStore.lifetimeID { return "Buy once for \(selected.displayPrice)" }
        if selected.id == PlusStore.annualID, store.annualTrialEligible, trialLength(selected) != nil { return "Start free trial" }
        return "Subscribe"
    }

    private var terms: some View {
        Text(selected?.id == PlusStore.lifetimeID
             ? "One payment, charged to your Apple Account. Plus stays unlocked on your Apple devices."
             : "Charged to your Apple Account. The subscription renews automatically at the same price unless you cancel at least 24 hours before the period ends. Manage or cancel any time in Settings → Apple Account → Subscriptions.")
            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var ownedPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("You have Plus", systemImage: "checkmark.seal.fill").font(.brake(.headline))
                .foregroundStyle(BrakeTheme.accent)
            Text("Thanks for supporting BrakeCheck.").font(.brake(.subheadline))
            Link("Manage subscription", destination: URL(string: "https://apps.apple.com/account/subscriptions")!)
                .font(.brake(.subheadline, weight: .semibold))
        }
        .brakePanel()
    }

    private var unavailablePanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Plans aren't available right now.").font(.brake(.headline))
            Text("Check your connection and try again. Safety features keep working without Plus.")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            Button("Try again") { Task { await store.load() } }.font(.brake(.subheadline, weight: .semibold))
        }
        .brakePanel()
    }

    private var footerLinks: some View {
        HStack(spacing: 18) {
            Button("Restore Purchases") { Task { await store.restore() } }
            Link("Terms of Use", destination: LegalLinks.termsOfUse)
            if let privacy = LegalLinks.privacyPolicy { Link("Privacy Policy", destination: privacy) }
        }
        .font(.brake(.caption, weight: .semibold))
        .foregroundStyle(BrakeTheme.accent)
        .frame(maxWidth: .infinity)
    }
}
