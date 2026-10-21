import BrakeCheckCore
import SwiftUI

/// The Premium paywall, and the Premium page for people who already pay.
/// Shown after the practice drive in onboarding, after the first saved trip, and from locked
/// features, never before the driver has seen a warning work (pricing doc, section 7).
/// Every price comes from the App Store; this view only derives "about $x/month" and "Save y%".
struct PremiumView: View {
    enum Context { case browse, onboarding, firstTrip, history }

    @ObservedObject private var store = PremiumStore.shared
    @AppStorage("display.premiumCompactBanner") private var compactBanner = false
    @State private var selected: PremiumPlan = .annual
    private let context: Context
    private let onClose: (() -> Void)?

    init(context: Context = .browse, onClose: (() -> Void)? = nil) {
        self.context = context
        self.onClose = onClose
    }

    /// The first-trip screen: closing it opens the trip summary.
    init(onContinue: (() -> Void)?) {
        self.init(context: .firstTrip, onClose: onContinue)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                if store.isSubscribed {
                    activeSection
                } else {
                    benefits
                    alwaysFree
                    purchaseSection
                }
                footer
            }
            .frame(maxWidth: 560, alignment: .leading)
            .padding(.horizontal, 22)
            .padding(.top, onClose == nil ? 20 : 4)
            .padding(.bottom, 28)
            .frame(maxWidth: .infinity)
        }
        .background(BrakeTheme.background.ignoresSafeArea())
        .foregroundStyle(BrakeTheme.text)
        .accessibilityIdentifier("premiumPreview")
        .safeAreaInset(edge: .top) {
            if let onClose { closeBar(onClose) }
        }
        .task { await store.load() }
    }

    // MARK: - Sections

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("BRAKESENSE PREMIUM")
                .font(.brake(size: 12, weight: .bold))
                .tracking(1.4)
                .foregroundStyle(BrakeTheme.accentText)
            Text(title)
                .font(.brake(size: 32, weight: .bold))
                .fixedSize(horizontal: false, vertical: true)
            Text(subtitle)
                .font(.brake(.body))
                .foregroundStyle(BrakeTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var title: String {
        if store.isSubscribed { return "Premium is on" }
        switch context {
        case .onboarding: return "Know the moment traffic moves"
        case .firstTrip: return "Your first drive is saved"
        case .history: return "Keep every drive"
        case .browse: return "Get more from every drive"
        }
    }

    private var subtitle: String {
        if store.isSubscribed { return "Everything below is unlocked on this Apple Account." }
        switch context {
        case .firstTrip: return "Premium adds Go alerts and keeps your whole trip history, with weekly trends."
        case .history: return "Free keeps your last 7 days. Premium brings back every saved trip and adds weekly trends."
        case .onboarding, .browse: return "Go alerts, your whole trip history with weekly trends, and display options."
        }
    }

    private var benefits: some View {
        VStack(alignment: .leading, spacing: 12) {
            benefit("car.side.arrowtriangle.up.arrowtriangle.down", "Go alerts",
                    "A heads-up when the car ahead pulls away or your light turns green.")
            benefit("chart.xyaxis.line", "Every trip, with trends",
                    "All your drives, not just the last 7 days, plus your weekly hard-brake trend.")
            benefit("rectangle.compress.vertical", "Display options",
                    "A compact turn banner that shows more of the road.")
        }
    }

    private var alwaysFree: some View {
        Label {
            Text("Always free: collision warnings, crash check-ins and your dash cam footage.")
                .fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: "checkmark.shield.fill")
        }
        .font(.brake(.subheadline, weight: .semibold))
        .foregroundStyle(BrakeTheme.accentText)
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    @ViewBuilder private var purchaseSection: some View {
        if store.offers.isEmpty {
            if store.isLoadingProduct {
                ProgressView("Checking the App Store")
                    .font(.brake(.subheadline))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            } else {
                // The App Store sets the price; until the products load, never show one.
                VStack(alignment: .leading, spacing: 8) {
                    Text("Premium can't load from the App Store right now.")
                        .font(.brake(.subheadline, weight: .semibold))
                    Button("Try again") { Task { await store.load() } }
                        .font(.brake(.subheadline, weight: .semibold))
                        .foregroundStyle(BrakeTheme.accentText)
                }
            }
        } else {
            VStack(spacing: 10) {
                ForEach(PremiumPlan.allCases) { plan in
                    if let offer = store.offers[plan] { planRow(offer) }
                }
            }
            if selected == .annual, let annual = store.offers[.annual], let days = annual.trialDays {
                trialTimeline(annual, days: days)
            }
            purchaseButton
            Text(terms)
                .font(.brake(.caption))
                .foregroundStyle(BrakeTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var activeSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(activeLabel, systemImage: "checkmark.seal.fill")
                .font(.brake(.headline))
                .foregroundStyle(BrakeTheme.accentText)
            DrivingAssistControls(offerUpgrade: false)
                .padding(18)
                .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
            Toggle("Compact camera directions", isOn: $compactBanner)
                .font(.brake(.subheadline, weight: .semibold))
                .padding(18)
                .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18))
            if store.activePlan?.isSubscription == true {
                Button("Manage subscription") { Task { await store.manageSubscription() } }
                    .font(.brake(.subheadline, weight: .semibold))
                    .foregroundStyle(BrakeTheme.accentText)
            }
        }
    }

    private var activeLabel: String {
        switch store.activePlan {
        case .lifetime?: return "Lifetime Premium"
        case .monthly?: return "Monthly plan active"
        case .annual?, nil: return "Yearly plan active"
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let message = store.message {
                Text(message)
                    .font(.brake(.caption))
                    .foregroundStyle(BrakeTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 20) {
                Button("Restore purchases") { Task { await store.restore() } }
                    .disabled(store.isBusy)
                if !store.isSubscribed {
                    Button("Redeem code") { Task { await store.redeemOfferCode() } }
                        .accessibilityIdentifier("paywall.redeem")
                }
            }
            HStack(spacing: 20) {
                Link("Terms of Use", destination: LegalLinks.termsOfUse)
                if let privacy = LegalLinks.privacyPolicy {
                    Link("Privacy Policy", destination: privacy)
                }
            }
        }
        .font(.brake(.caption, weight: .semibold))
        .foregroundStyle(BrakeTheme.accentText)
    }

    // MARK: - Plans

    private func planRow(_ offer: PremiumOffer) -> some View {
        let isSelected = selected == offer.plan
        return Button {
            withAnimation(.easeOut(duration: 0.15)) { selected = offer.plan }
        } label: {
            HStack(alignment: .center, spacing: 14) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.brake(size: 22, weight: .semibold))
                    .foregroundStyle(isSelected ? BrakeTheme.accentText : BrakeTheme.muted)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(planTitle(offer.plan)).font(.brake(.headline))
                        if let badge = badge(for: offer) {
                            Text(badge)
                                .font(.brake(size: 11, weight: .bold))
                                .padding(.horizontal, 8).padding(.vertical, 3)
                                .foregroundStyle(BrakeTheme.background)
                                .background(BrakeTheme.accentText, in: Capsule())
                        }
                    }
                    Text(planDetail(offer))
                        .font(.brake(.subheadline))
                        .foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(offer.displayPrice).font(.brake(.headline)).monospacedDigit()
                    Text(periodLabel(offer.plan)).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
            }
            .padding(16)
            .background(isSelected ? BrakeTheme.accent.opacity(0.12) : BrakeTheme.panel,
                        in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(isSelected ? BrakeTheme.accentText : BrakeTheme.text.opacity(0.08), lineWidth: isSelected ? 1.5 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("plan." + offer.plan.rawValue)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    private func planTitle(_ plan: PremiumPlan) -> String {
        switch plan {
        case .annual: return "Yearly"
        case .monthly: return "Monthly"
        case .lifetime: return "Lifetime"
        }
    }

    private func periodLabel(_ plan: PremiumPlan) -> String {
        switch plan {
        case .annual: return "per year"
        case .monthly: return "per month"
        case .lifetime: return "one time"
        }
    }

    private func planDetail(_ offer: PremiumOffer) -> String {
        switch offer.plan {
        case .annual:
            let perMonth = offer.formatted(PremiumPricing.monthlyEquivalent(annual: offer.price))
            if let days = offer.trialDays { return "\(days) days free, then about \(perMonth)/month" }
            return "About \(perMonth)/month, billed yearly"
        case .monthly: return "Billed monthly. Cancel anytime."
        case .lifetime: return "Pay once. Includes future Premium features."
        }
    }

    private func badge(for offer: PremiumOffer) -> String? {
        guard offer.plan == .annual, let monthly = store.offers[.monthly],
              let percent = PremiumPricing.annualSavingsPercent(annual: offer.price, monthly: monthly.price) else { return nil }
        return "Save \(percent)%"
    }

    private func trialTimeline(_ offer: PremiumOffer, days: Int) -> some View {
        let now = Date()
        let remind = TrialReminder.fireDate(trialStart: now, trialDays: days)
        let charge = TrialReminder.chargeDate(trialStart: now, trialDays: days)
        return VStack(alignment: .leading, spacing: 14) {
            timelineStep("lock.open.fill", "Today", "Full Premium, free")
            if let remind {
                timelineStep("bell.fill", "Day \(days - 2) · \(shortDate(remind))", "We remind you the trial is ending")
            }
            if let charge {
                timelineStep("creditcard.fill", "Day \(days) · \(shortDate(charge))",
                             "\(offer.displayPrice) for the year, unless you cancel before")
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("paywall.trialTimeline")
    }

    private func timelineStep(_ icon: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.brake(size: 14, weight: .semibold))
                .foregroundStyle(BrakeTheme.background)
                .frame(width: 28, height: 28)
                .background(BrakeTheme.accentText, in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.brake(.subheadline, weight: .bold))
                Text(detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private func shortDate(_ date: Date) -> String {
        date.formatted(.dateTime.month(.abbreviated).day())
    }

    private var purchaseButton: some View {
        Button {
            Task { await store.purchase(selected) }
        } label: {
            Text(store.isBusy ? "Connecting to the App Store…" : buttonTitle)
                .font(.brake(size: 17, weight: .bold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 16)
                .foregroundStyle(BrakeTheme.background)
                .background(BrakeTheme.accent, in: Capsule())
                .shadow(color: BrakeTheme.buttonShadow, radius: 10, y: 4)
        }
        .buttonStyle(.plain)
        .disabled(store.isBusy || store.offers[selected] == nil)
        .accessibilityIdentifier("paywall.purchase")
    }

    private var buttonTitle: String {
        guard let offer = store.offers[selected] else { return "Continue" }
        switch offer.plan {
        case .annual:
            if let days = offer.trialDays { return "Start \(days)-day free trial" }
            return "Subscribe for \(offer.displayPrice)/year"
        case .monthly: return "Subscribe for \(offer.displayPrice)/month"
        case .lifetime: return "Buy Lifetime for \(offer.displayPrice)"
        }
    }

    /// Apple requires the price, period and renewal terms next to the purchase button.
    private var terms: String {
        guard let offer = store.offers[selected] else { return "" }
        let cancel = "Cancel in Settings > your name > Subscriptions"
        switch offer.plan {
        case .annual:
            if let days = offer.trialDays {
                return "Free for \(days) days, then \(offer.displayPrice) a year. Renews automatically until you cancel. \(cancel) at least 24 hours before the trial ends and you won't be charged."
            }
            return "\(offer.displayPrice) a year. Renews automatically until you cancel. \(cancel) at least 24 hours before the renewal date."
        case .monthly:
            return "\(offer.displayPrice) a month. Renews automatically until you cancel. \(cancel) at least 24 hours before the renewal date."
        case .lifetime:
            return "A one-time purchase of \(offer.displayPrice). No subscription, nothing renews."
        }
    }

    // MARK: - Pieces

    private func benefit(_ icon: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon)
                .font(.brake(size: 20, weight: .semibold))
                .foregroundStyle(BrakeTheme.accentText)
                .frame(width: 32)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.brake(.headline))
                Text(detail)
                    .font(.brake(.subheadline))
                    .foregroundStyle(BrakeTheme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private func closeBar(_ close: @escaping () -> Void) -> some View {
        HStack {
            Spacer()
            Button(action: close) {
                Text(store.isSubscribed ? "Done" : (context == .firstTrip ? "See my trip" : "Not now"))
                    .font(.brake(.subheadline, weight: .semibold))
                    .foregroundStyle(BrakeTheme.muted)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(BrakeTheme.panel, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("paywall.notNow")
        }
        .padding(.horizontal, 18).padding(.top, 8).padding(.bottom, 4)
        .background(BrakeTheme.background)
    }
}
