import Foundation

/// Core Location may deliver several fixes together. Rejecting the entire
/// batch because its newest fix is inaccurate can needlessly pause guidance.
public struct NavigationLocationSample: Sendable {
    public let timestamp: TimeInterval
    public let horizontalAccuracy: Double
    public let coordinateValid: Bool

    public init(timestamp: TimeInterval, horizontalAccuracy: Double, coordinateValid: Bool) {
        self.timestamp = timestamp
        self.horizontalAccuracy = horizontalAccuracy
        self.coordinateValid = coordinateValid
    }
}

public enum NavigationLocationSelection {
    public static func newestFreshIndex(_ samples: [NavigationLocationSample], now: TimeInterval,
                                        eligibleAfter: TimeInterval, lastAccepted: TimeInterval?) -> Int? {
        guard now.isFinite, eligibleAfter.isFinite else { return nil }
        return samples.indices.filter { index in
            let sample = samples[index]
            return sample.coordinateValid && sample.timestamp.isFinite
                && sample.timestamp >= eligibleAfter && sample.timestamp <= now
                // Core Location can batch or briefly delay updates. These are
                // useful for the map; turn progress applies stricter limits.
                && now - sample.timestamp <= 5
                && (lastAccepted.map { sample.timestamp > $0 } ?? true)
                && sample.horizontalAccuracy.isFinite
                && (0...65).contains(sample.horizontalAccuracy)
        }.max { first, second in
            let left = samples[first], right = samples[second]
            return left.timestamp == right.timestamp
                ? left.horizontalAccuracy > right.horizontalAccuracy
                : left.timestamp < right.timestamp
        }
    }
}
