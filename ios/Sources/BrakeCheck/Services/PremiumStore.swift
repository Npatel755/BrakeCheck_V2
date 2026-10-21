import BrakeCheckCore
import Foundation
import StoreKit
import UIKit
import UserNotifications

/// One plan as the paywall shows it. Prices come from the App Store product. Screenshot builds
/// (the SCREENSHOTS flag, set only by .github/workflows/ios-screens.yml) fill in the planned US
/// prices instead, because the Simulator has no App Store products to load.
struct PremiumOffer: Identifiable {
    let plan: PremiumPlan
    let displayPrice: String
    let price: Decimal
    let format: Decimal.FormatStyle.Currency
    /// Free-trial length in days, only while this Apple Account can still get the trial.
    let trialDays: Int?
    fileprivate let product: Product?
    var id: String { plan.rawValue }
    func formatted(_ value: Decimal) -> String { value.formatted(format) }
}

@MainActor final class PremiumStore: ObservableObject {
    static let shared = PremiumStore()
    static let trialReminderID = "premium.trialReminder"

    @Published private(set) var offers: [PremiumPlan: PremiumOffer] = [:]
    @Published private(set) var isLoadingProduct = false
    /// True for any active Premium purchase: the yearly or monthly subscription, or Lifetime.
    @Published private(set) var isSubscribed = false
    @Published private(set) var activePlan: PremiumPlan?
    @Published private(set) var isBusy = false
    @Published var message: String?
    private var updates: Task<Void, Never>?

    private init() {
        updates = Task { [weak self] in
            for await result in Transaction.updates {
                guard let self else { return }
                if case .verified(let transaction) = result { await transaction.finish() }
                await self.refreshEntitlement()
            }
        }
        Task { await load() }
    }

    deinit { updates?.cancel() }

    func load() async {
        await refreshEntitlement()
        guard !isLoadingProduct else { return }
        isLoadingProduct = true
        defer { isLoadingProduct = false }
        var next: [PremiumPlan: PremiumOffer] = [:]
        do {
            for product in try await Product.products(for: PremiumPlan.allCases.map(\.productID)) {
                guard let plan = PremiumPlan(productID: product.id) else { continue }
                next[plan] = await Self.offer(plan, product)
            }
        } catch {
            message = "The App Store could not load Premium. Try again later."
        }
        #if SCREENSHOTS
        if next.isEmpty { next = Self.screenshotOffers; message = nil }
        #endif
        offers = next
    }

    func purchase(_ plan: PremiumPlan) async {
        guard let offer = offers[plan], !isBusy else { return }
        guard let product = offer.product else {
            message = "Purchases aren't available in this build."
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            switch try await product.purchase() {
            case .success(.verified(let transaction)):
                await transaction.finish()
                await refreshEntitlement()
                if let days = offer.trialDays {
                    await scheduleTrialReminder(start: transaction.purchaseDate, trialDays: days,
                                                price: offer.displayPrice)
                }
            case .success(.unverified):
                message = "The purchase could not be verified. No Premium access was granted."
            case .pending:
                message = "Purchase pending approval. Premium will activate when the App Store confirms it."
            case .userCancelled: break
            @unknown default: break
            }
        } catch {
            message = "Purchase was not completed: \(error.localizedDescription)"
        }
    }

    func restore() async {
        guard !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            try await AppStore.sync()
            await refreshEntitlement()
            if !isSubscribed { message = "No active Premium purchase was found for this Apple Account." }
        } catch {
            message = "Could not restore purchases: \(error.localizedDescription)"
        }
    }

    /// Apple's own sheet for offer codes (the waitlist code, creator codes). A redeemed code
    /// arrives through Transaction.updates like any purchase.
    func redeemOfferCode() async {
        guard let scene = Self.activeScene else { return }
        do { try await AppStore.presentOfferCodeRedeemSheet(in: scene) } catch {
            message = "Couldn't open code redemption: \(error.localizedDescription)"
        }
    }

    func manageSubscription() async {
        guard let scene = Self.activeScene else { return }
        do { try await AppStore.showManageSubscriptions(in: scene) } catch {
            message = "Couldn't open subscription settings: \(error.localizedDescription)"
        }
    }

    private static var activeScene: UIWindowScene? {
        UIApplication.shared.connectedScenes.first { $0.activationState == .foregroundActive } as? UIWindowScene
    }

    private func refreshEntitlement() async {
        var active: PremiumPlan?
        for await result in Transaction.currentEntitlements {
            guard case .verified(let transaction) = result,
                  let plan = PremiumPlan(productID: transaction.productID),
                  transaction.revocationDate == nil, !transaction.isUpgraded,
                  transaction.expirationDate.map({ $0 > Date() }) ?? true else { continue }
            // Lifetime outranks a subscription bought on the same account.
            if active == nil || plan == .lifetime { active = plan }
        }
        #if SCREENSHOTS
        if ProcessInfo.processInfo.arguments.contains("-screenshotPremiumActive") { active = .annual }
        #endif
        activePlan = active
        isSubscribed = active != nil
    }

    private static func offer(_ plan: PremiumPlan, _ product: Product) async -> PremiumOffer {
        var trialDays: Int?
        if let subscription = product.subscription, let intro = subscription.introductoryOffer,
           intro.paymentMode == .freeTrial, await subscription.isEligibleForIntroOffer {
            trialDays = days(in: intro.period)
        }
        return PremiumOffer(plan: plan, displayPrice: product.displayPrice, price: product.price,
                            format: product.priceFormatStyle, trialDays: trialDays, product: product)
    }

    private static func days(in period: Product.SubscriptionPeriod) -> Int? {
        switch period.unit {
        case .day: return period.value
        case .week: return period.value * 7
        case .month: return period.value * 30
        case .year: return period.value * 365
        @unknown default: return nil
        }
    }

    /// The paywall promises a reminder before the trial ends. Without notification permission
    /// there is no reminder, so say when the trial ends instead.
    private func scheduleTrialReminder(start: Date, trialDays: Int, price: String) async {
        guard let fire = TrialReminder.fireDate(trialStart: start, trialDays: trialDays),
              let charge = TrialReminder.chargeDate(trialStart: start, trialDays: trialDays),
              fire > Date() else { return }
        let chargeDay = charge.formatted(date: .abbreviated, time: .omitted)
        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        guard granted else {
            message = "Notifications are off, so we can't remind you. Your free trial ends \(chargeDay). To avoid the charge, cancel before then in Settings > your name > Subscriptions."
            return
        }
        let content = UNMutableNotificationContent()
        content.title = "Your Premium trial ends in 2 days"
        content.body = "If you keep Premium, the yearly plan (\(price)) starts \(chargeDay). To cancel, go to Settings > your name > Subscriptions."
        var when = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: fire)
        // A reminder at 11 pm or 6 am gets missed; daytime it is.
        if let hour = when.hour, !(9...20).contains(hour) { when.hour = 10; when.minute = 0 }
        let request = UNNotificationRequest(identifier: Self.trialReminderID, content: content,
                                            trigger: UNCalendarNotificationTrigger(dateMatching: when, repeats: false))
        try? await center.add(request)
    }

    #if SCREENSHOTS
    /// Planned US launch prices, for Simulator screenshots only. Never part of an App Store build.
    private static var screenshotOffers: [PremiumPlan: PremiumOffer] {
        let usd = Decimal.FormatStyle.Currency(code: "USD", locale: Locale(identifier: "en_US"))
        func offer(_ plan: PremiumPlan, _ price: String, trial: Int? = nil) -> PremiumOffer {
            let value = Decimal(string: price) ?? 0
            return PremiumOffer(plan: plan, displayPrice: value.formatted(usd), price: value, format: usd,
                                trialDays: trial, product: nil)
        }
        return [.annual: offer(.annual, "29.99", trial: 7), .monthly: offer(.monthly, "4.99"),
                .lifetime: offer(.lifetime, "59.99")]
    }
    #endif
}
