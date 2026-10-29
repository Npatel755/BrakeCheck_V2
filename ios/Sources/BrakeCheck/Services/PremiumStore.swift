import Foundation
import StoreKit

@MainActor final class PremiumStore: ObservableObject {
    static let shared = PremiumStore()
    static let monthlyID = "com.brakecheck.prototype.premium.monthly"
    static var localTestingAvailable: Bool {
        #if BRAKECHECK_PREMIUM_TESTING
        true
        #else
        false
        #endif
    }

    @Published private(set) var product: Product?
    @Published private(set) var isLoadingProduct = false
    @Published private(set) var isSubscribed = false
    @Published private(set) var localTestingEnabled = false
    @Published private(set) var isBusy = false
    @Published var message: String?
    private var updates: Task<Void, Never>?
    private var verifiedSubscription = false

    private init() {
        #if BRAKECHECK_PREMIUM_TESTING
        localTestingEnabled = UserDefaults.standard.bool(forKey: "premium.localTestAccess")
        isSubscribed = localTestingEnabled
        #endif
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
        guard !Self.localTestingAvailable else { return }
        guard !isLoadingProduct else { return }
        isLoadingProduct = true
        defer { isLoadingProduct = false }
        do {
            product = try await Product.products(for: [Self.monthlyID]).first
        } catch {
            message = "The App Store could not load Premium. Try again later."
        }
    }

    func purchase() async {
        guard !Self.localTestingAvailable else { return }
        guard let product, !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            switch try await product.purchase() {
            case .success(.verified(let transaction)):
                await transaction.finish()
                await refreshEntitlement()
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
        guard !Self.localTestingAvailable else { return }
        guard !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            try await AppStore.sync()
            await refreshEntitlement()
            if !isSubscribed { message = "No active Premium subscription was found for this Apple Account." }
        } catch {
            message = "Could not restore purchases: \(error.localizedDescription)"
        }
    }

    private func refreshEntitlement() async {
        var active = false
        for await result in Transaction.currentEntitlements {
            guard case .verified(let transaction) = result,
                  transaction.productID == Self.monthlyID,
                  transaction.revocationDate == nil, !transaction.isUpgraded,
                  transaction.expirationDate.map({ $0 > Date() }) ?? true else { continue }
            active = true
        }
        verifiedSubscription = active
        updateAccess()
    }

    func setLocalTestingEnabled(_ enabled: Bool) {
        #if BRAKECHECK_PREMIUM_TESTING
        localTestingEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: "premium.localTestAccess")
        message = nil
        updateAccess()
        #endif
    }

    private func updateAccess() {
        isSubscribed = verifiedSubscription || (Self.localTestingAvailable && localTestingEnabled)
    }
}
