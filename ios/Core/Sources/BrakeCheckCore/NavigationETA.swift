import Foundation

public struct NavigationETA: Equatable, Sendable {
    public let remainingTime: TimeInterval
    public let baselineRemainingTime: TimeInterval
    public let confidence: Double
    public let usesObservedPace: Bool
    public func arrivalDate(from now: Date) -> Date { now.addingTimeInterval(remainingTime) }
}

/// Uses accepted measured route progress, including time spent stopped. Raw or
/// estimated map positions are deliberately not accepted by this API.
public struct NavigationETAEstimator {
    private struct Sample { let remaining: Double; let timestamp: TimeInterval }
    private var samples: [Sample] = []
    private var lastETA: Double?
    public init() {}
    public mutating func reset() { samples = []; lastETA = nil }
    public mutating func update(remainingDistance: Double, routeDistance: Double,
                               expectedTravelTime: TimeInterval, timestamp: TimeInterval,
                               historicalMultiplier: Double = 1) -> NavigationETA? {
        guard remainingDistance.isFinite, remainingDistance >= 0, routeDistance.isFinite, routeDistance > 0,
              remainingDistance <= routeDistance + 1, expectedTravelTime.isFinite, expectedTravelTime >= 0,
              timestamp.isFinite, timestamp >= 0,
              samples.last.map({ timestamp > $0.timestamp && remainingDistance <= $0.remaining + 1 }) ?? true else { return nil }
        let multiplier = historicalMultiplier.isFinite ? min(4, max(0.5, historicalMultiplier)) : 1
        let baseline = expectedTravelTime * min(1, remainingDistance / routeDistance) * multiplier
        samples.append(Sample(remaining: remainingDistance, timestamp: timestamp))
        samples.removeAll { timestamp - $0.timestamp > 120 }
        if samples.count > 180 { samples.removeFirst(samples.count - 180) }
        var estimate = baseline
        var confidence = 0.35
        var usesPace = false
        if let first = samples.first, timestamp - first.timestamp >= 15 {
            let elapsed = timestamp - first.timestamp
            let advanced = max(0, first.remaining - remainingDistance)
            let baselineSpeed = expectedTravelTime > 0 ? routeDistance / expectedTravelTime : 13.89
            // Stops contribute to elapsed time, with a bounded minimum speed so
            // a red light cannot turn a five-minute trip into an infinite ETA.
            let observedSpeed = max(baselineSpeed / 4, min(65, advanced / elapsed))
            let observedTime = remainingDistance / max(0.5, observedSpeed)
            let weight = min(0.75, elapsed / 120)
            estimate = baseline * (1 - weight) + observedTime * weight
            confidence = min(0.9, 0.4 + elapsed / 240); usesPace = true
        }
        if remainingDistance == 0 { estimate = 0 }
        else if let lastETA { estimate = lastETA * 0.65 + estimate * 0.35 }
        lastETA = estimate
        return NavigationETA(remainingTime: max(0, estimate), baselineRemainingTime: baseline,
            confidence: confidence, usesObservedPace: usesPace)
    }
}

public struct NavigationTrafficObservation: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let routeKey: String
    public let departure: Date
    public let duration: TimeInterval
    public let baselineDuration: TimeInterval
    public let distance: Double
    public init(id: UUID = UUID(), routeKey: String, departure: Date, duration: TimeInterval,
                baselineDuration: TimeInterval, distance: Double) {
        self.id = id; self.routeKey = routeKey; self.departure = departure; self.duration = duration
        self.baselineDuration = baselineDuration; self.distance = distance
    }
    public var isValid: Bool {
        !routeKey.isEmpty && departure.timeIntervalSince1970.isFinite
            && duration.isFinite && (30...86_400).contains(duration)
            && baselineDuration.isFinite && baselineDuration >= 30
            && distance.isFinite && distance >= 100
            && (0.25...6).contains(duration / baselineDuration)
    }
}

public struct NavigationTrafficPattern: Equatable, Sendable {
    public let multiplier: Double
    public let sampleCount: Int
    public let confidence: Double
}

public enum NavigationTrafficHistory {
    public static func normalized(_ observations: [NavigationTrafficObservation], now: Date = Date()) -> [NavigationTrafficObservation] {
        var seen: Set<UUID> = []
        return Array(observations.filter {
            $0.isValid && (0...(180 * 86_400)).contains(now.timeIntervalSince($0.departure)) && seen.insert($0.id).inserted
        }.sorted { $0.departure > $1.departure }.prefix(500))
    }
    /// Local observations are a historical estimate, never a live traffic feed.
    /// Require three comparable trips on the same route, weekday class and hour.
    public static func pattern(for routeKey: String, departure: Date, observations: [NavigationTrafficObservation],
                               calendar: Calendar = .current, now: Date = Date()) -> NavigationTrafficPattern? {
        let hour = calendar.component(.hour, from: departure)
        let weekend = calendar.isDateInWeekend(departure)
        let matches = normalized(observations, now: now).filter {
            $0.routeKey == routeKey && calendar.isDateInWeekend($0.departure) == weekend
                && calendar.component(.hour, from: $0.departure) == hour
        }
        guard matches.count >= 3 else { return nil }
        let ratios = matches.map { $0.duration / $0.baselineDuration }.sorted()
        let median = ratios[ratios.count / 2]
        let deviation = ratios.reduce(0) { $0 + abs($1 - median) } / Double(ratios.count)
        return NavigationTrafficPattern(multiplier: min(4, max(0.5, median)), sampleCount: matches.count,
            confidence: min(0.85, Double(matches.count) / 12) / (1 + deviation))
    }
}

public struct NavigationTravelPlan: Equatable, Sendable {
    public let departure: Date
    public let arrival: Date
    public let travelTime: TimeInterval
    public let usesHistoricalTraffic: Bool
    public static func leaving(at departure: Date, duration: TimeInterval, historicalMultiplier: Double? = nil,
                               stopTime: TimeInterval = 0) -> NavigationTravelPlan? {
        guard departure.timeIntervalSince1970.isFinite, duration.isFinite, duration >= 0,
              stopTime.isFinite, stopTime >= 0,
              historicalMultiplier.map({ $0.isFinite && $0 > 0 }) ?? true else { return nil }
        let time = duration * min(4, max(0.5, historicalMultiplier ?? 1)) + stopTime
        return NavigationTravelPlan(departure: departure, arrival: departure.addingTimeInterval(time),
            travelTime: time, usesHistoricalTraffic: historicalMultiplier != nil)
    }
    public static func arriving(at arrival: Date, duration: TimeInterval, historicalMultiplier: Double? = nil,
                                stopTime: TimeInterval = 0) -> NavigationTravelPlan? {
        guard let forward = leaving(at: arrival, duration: duration,
                                    historicalMultiplier: historicalMultiplier, stopTime: stopTime) else { return nil }
        return NavigationTravelPlan(departure: arrival.addingTimeInterval(-forward.travelTime), arrival: arrival,
            travelTime: forward.travelTime, usesHistoricalTraffic: forward.usesHistoricalTraffic)
    }
}
