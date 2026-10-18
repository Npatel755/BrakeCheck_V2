import BrakeCheckCore
import StoreKit
import SwiftUI
import UserNotifications

/// BrakeCheck Plus through StoreKit 2: no third-party SDK and no server. Apple verifies each
/// transaction on the phone, and entitlements come from `Transaction.currentEntitlements`.
/// The products are created in App Store Connect under these IDs (one subscription group for
/// monthly and annual, plus a non-consumable lifetime unlock). Until they exist, the paywall
/// says plans aren't available.
@MainActor
final class PlusStore: ObservableObject {
    static let annualID = "brakecheck.plus.annual"
    static let monthlyID = "brakecheck.plus.monthly"
    static let lifetimeID = "brakecheck.plus.lifetime"
    /// Display order on the paywall: annual first, then monthly, then lifetime.
    static let productIDs = [annualID, monthlyID, lifetimeID]

    @Published private(set) var products: [Product] = []
    @Published private(set) var isPlus = false
    @Published private(set) var loading = true
    @Published private(set) var purchasing = false
    @Published private(set) var annualTrialEligible = false
    @Published var message: String?

    private var updates: Task<Void, Never>?

    init() {
        // Purchases made elsewhere (another device, Ask to Buy approval, a renewal) arrive here.
        updates = Task { [weak self] in
            for await result in Transaction.updates {
                if case .verified(let transaction) = result { await transaction.finish() }
                await self?.refreshEntitlement()
            }
        }
        Task { await load() }
    }

    func product(_ id: String) -> Product? { products.first { $0.id == id } }

    func load() async {
        loading = true
        defer { loading = false }
        if let found = try? await Product.products(for: Self.productIDs) {
            products = Self.productIDs.compactMap { id in found.first { $0.id == id } }
        }
        if let subscription = product(Self.annualID)?.subscription, subscription.introductoryOffer != nil {
            annualTrialEligible = await subscription.isEligibleForIntroOffer
        } else {
            annualTrialEligible = false
        }
        await refreshEntitlement()
    }

    func refreshEntitlement() async {
        var active = false
        for await result in Transaction.currentEntitlements {
            if case .verified(let transaction) = result,
               Self.productIDs.contains(transaction.productID),
               transaction.revocationDate == nil {
                active = true
            }
        }
        isPlus = active
    }

    /// Returns true when the purchase unlocked Plus.
    func buy(_ product: Product) async -> Bool {
        purchasing = true
        defer { purchasing = false }
        do {
            switch try await product.purchase() {
            case .success(let verification):
                guard case .verified(let transaction) = verification else {
                    message = "The App Store couldn't verify this purchase. You haven't been charged for Plus."
                    return false
                }
                await transaction.finish()
                if transaction.offerType == .introductory, let offer = product.subscription?.introductoryOffer {
                    await scheduleTrialReminder(start: transaction.purchaseDate, offer: offer)
                }
                await refreshEntitlement()
                annualTrialEligible = false
                return isPlus
            case .pending:
                message = "Your purchase is waiting for approval. Plus unlocks as soon as it's approved."
                return false
            case .userCancelled:
                return false
            @unknown default:
                return false
            }
        } catch {
            message = "The purchase didn't go through: \(error.localizedDescription)"
            return false
        }
    }

    func restore() async {
        do {
            try await AppStore.sync()
        } catch {
            message = "Couldn't reach the App Store: \(error.localizedDescription)"
            return
        }
        await refreshEntitlement()
        message = isPlus ? "Plus is restored." : "No Plus purchase was found for this Apple Account."
    }

    /// The paywall promises a reminder before the trial ends, so schedule one. It needs notification
    /// permission, asked for here, right after the person chose a trial.
    private func scheduleTrialReminder(start: Date, offer: Product.SubscriptionOffer) async {
        guard offer.paymentMode == .freeTrial,
              let days = Self.days(in: offer.period),
              let reminderDay = PlusPolicy.trialReminderDay(trialDays: days) else { return }
        let center = UNUserNotificationCenter.current()
        guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
        let content = UNMutableNotificationContent()
        content.title = "Your Plus trial ends in 2 days"
        content.body = "You'll be charged on \((start.addingTimeInterval(Double(days) * 86_400)).formatted(date: .abbreviated, time: .omitted)) unless you cancel in Settings → Apple Account → Subscriptions."
        let fireDate = start.addingTimeInterval(Double(reminderDay) * 86_400)
        let interval = max(fireDate.timeIntervalSinceNow, 60)
        let request = UNNotificationRequest(identifier: "plus.trialReminder", content: content,
                                            trigger: UNTimeIntervalNotificationTrigger(timeInterval: interval, repeats: false))
        try? await center.add(request)
    }

    static func days(in period: Product.SubscriptionPeriod) -> Int? {
        switch period.unit {
        case .day: return period.value
        case .week: return period.value * 7
        case .month: return period.value * 30
        case .year: return period.value * 365
        @unknown default: return nil
        }
    }
}
