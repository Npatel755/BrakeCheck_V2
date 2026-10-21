import Foundation

/// The three ways to buy Premium. The product IDs become permanent once they exist in
/// App Store Connect, so change them only before the products are created there.
public enum PremiumPlan: String, CaseIterable, Identifiable, Codable {
    case annual, monthly, lifetime
    public var id: String { rawValue }
    public var productID: String { "com.brakesense.premium." + rawValue }
    public var isSubscription: Bool { self != .lifetime }
    public init?(productID: String) {
        guard let plan = Self.allCases.first(where: { $0.productID == productID }) else { return nil }
        self = plan
    }
}

/// Paywall arithmetic, kept out of the view so it can be tested. Prices always come from the
/// App Store; these only derive the "about $x/month" and "Save y%" lines from them.
public enum PremiumPricing {
    /// The yearly price spread over 12 months, rounded to the cent.
    public static func monthlyEquivalent(annual: Decimal) -> Decimal {
        rounded(annual / 12, scale: 2)
    }

    /// Whole-percent saving of the yearly plan against 12 monthly payments, or nil when the
    /// yearly plan isn't cheaper (then the paywall shows no saving at all).
    public static func annualSavingsPercent(annual: Decimal, monthly: Decimal) -> Int? {
        guard annual > 0, monthly > 0 else { return nil }
        let yearOfMonthly = monthly * 12
        guard annual < yearOfMonthly else { return nil }
        let percent = rounded((1 - annual / yearOfMonthly) * 100, scale: 0)
        let value = NSDecimalNumber(decimal: percent).intValue
        return value > 0 ? value : nil
    }

    private static func rounded(_ value: Decimal, scale: Int) -> Decimal {
        var input = value, output = Decimal()
        NSDecimalRound(&output, &input, scale, .plain)
        return output
    }
}

/// The trial reminder the paywall promises ("Day 5: we remind you"): two days before a
/// free trial turns into a paid year.
public enum TrialReminder {
    public static func fireDate(trialStart: Date, trialDays: Int, daysBefore: Int = 2,
                                calendar: Calendar = .current) -> Date? {
        guard trialDays > daysBefore, daysBefore >= 0 else { return nil }
        return calendar.date(byAdding: .day, value: trialDays - daysBefore, to: trialStart)
    }

    /// When the first payment is taken.
    public static func chargeDate(trialStart: Date, trialDays: Int, calendar: Calendar = .current) -> Date? {
        guard trialDays > 0 else { return nil }
        return calendar.date(byAdding: .day, value: trialDays, to: trialStart)
    }
}

/// Free plans show the last week of trips. Older trips stay saved on the phone, never deleted,
/// and reappear with Premium.
public enum TripHistoryPolicy {
    public static let freeWindowDays = 7

    public static func split(_ trips: [TripMetrics], now: Date, premium: Bool,
                             calendar: Calendar = .current) -> (visible: [TripMetrics], lockedCount: Int) {
        guard !premium else { return (trips, 0) }
        guard let cutoff = calendar.date(byAdding: .day, value: -freeWindowDays, to: now) else { return (trips, 0) }
        let visible = trips.filter { $0.startedAt >= cutoff }
        return (visible, trips.count - visible.count)
    }
}

/// One calendar week of driving, for the Premium trends card.
public struct TripTrendWeek: Equatable {
    public let weekStart: Date
    public let trips: Int
    public let drivingSeconds: Double
    public let hardBrakes: Int
    public let closeCalls: Int

    /// Estimated hard brakes per hour of driving; nil for a week with no driving time.
    public var hardBrakesPerHour: Double? {
        drivingSeconds >= 60 ? Double(hardBrakes) / (drivingSeconds / 3600) : nil
    }
}

public enum TripTrends {
    /// The last `weeks` calendar weeks, newest first, including weeks without trips.
    public static func weekly(_ trips: [TripMetrics], now: Date, weeks: Int = 4,
                              calendar: Calendar = .current) -> [TripTrendWeek] {
        guard weeks > 0, let current = calendar.dateInterval(of: .weekOfYear, for: now)?.start else { return [] }
        return (0..<weeks).compactMap { offset in
            guard let start = calendar.date(byAdding: .weekOfYear, value: -offset, to: current),
                  let end = calendar.date(byAdding: .weekOfYear, value: 1, to: start) else { return nil }
            let inWeek = trips.filter { $0.startedAt >= start && $0.startedAt < end }
            return TripTrendWeek(weekStart: start, trips: inWeek.count,
                                 drivingSeconds: inWeek.reduce(0) { $0 + $1.durationSeconds },
                                 hardBrakes: inWeek.reduce(0) { $0 + $1.estimatedLeadHardBrakes },
                                 closeCalls: inWeek.reduce(0) { $0 + $1.estimatedCloseCalls })
        }
    }
}
