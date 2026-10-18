import Foundation

/// The line between free and BrakeCheck Plus, in one place so it can change without touching views.
/// Safety features (warnings, the spoken alert, crash texts, saved incident footage) are never gated here.
public enum PlusPolicy {
    /// Free users see this many of their most recent trips. Older trips stay on the phone and
    /// reappear with Plus; nothing is deleted.
    public static let freeTripHistoryCount = 5

    /// Offer Plus at the end of onboarding. Off until the Plus features are built, so the
    /// paywall never sells something the app doesn't have yet (a common App Review rejection).
    public static let offerInOnboarding = false

    public static func visibleTripCount(total: Int, isPlus: Bool) -> Int {
        isPlus ? max(total, 0) : min(max(total, 0), freeTripHistoryCount)
    }

    public static func lockedTripCount(total: Int, isPlus: Bool) -> Int {
        max(total, 0) - visibleTripCount(total: total, isPlus: isPlus)
    }

    /// Days after a free trial starts to remind the user, two days before the first charge.
    /// Nil when the trial is too short for a useful reminder.
    public static func trialReminderDay(trialDays: Int) -> Int? {
        trialDays >= 3 ? trialDays - 2 : nil
    }

    /// The annual plan's saving against twelve monthly payments, as a whole percent.
    public static func annualSavingsPercent(monthly: Decimal, annual: Decimal) -> Int? {
        guard monthly > 0, annual > 0 else { return nil }
        let yearOfMonthly = monthly * 12
        guard annual < yearOfMonthly else { return nil }
        let saving = NSDecimalNumber(decimal: (yearOfMonthly - annual) / yearOfMonthly * 100).doubleValue
        return Int(saving.rounded(.down))
    }
}
