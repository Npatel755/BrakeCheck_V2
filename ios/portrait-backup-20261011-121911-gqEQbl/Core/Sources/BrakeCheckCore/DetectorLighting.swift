import Foundation

public enum DetectorLightingMode: String, CaseIterable, Hashable {
    case automatic, day, night
    public var title: String {
        switch self {
        case .automatic: return "Auto"
        case .day: return "Day"
        case .night: return "Night"
        }
    }
}

public enum DetectorLightingProfile: String {
    case day, night
    public var title: String { self == .day ? "Standard" : "Low light" }
}

/// Scene samples choose a detector, never a lead, lane or vehicle heading.
/// Median luminance limits the effect of a few bright headlights. Capture
/// exposure supplements it because auto exposure can brighten a dark scene.
public struct DetectorLightingSample {
    public let medianLuma: Double
    public let darkFraction: Double
    public let iso: Double?
    public let exposureSeconds: Double?
    public init(medianLuma: Double, darkFraction: Double,
                iso: Double? = nil, exposureSeconds: Double? = nil) {
        self.medianLuma = medianLuma; self.darkFraction = darkFraction
        self.iso = iso; self.exposureSeconds = exposureSeconds
    }
    public var isUsable: Bool {
        medianLuma.isFinite && (0...1).contains(medianLuma)
            && darkFraction.isFinite && (0...1).contains(darkFraction)
            && (iso.map { $0.isFinite && (0...100_000).contains($0) } ?? true)
            && (exposureSeconds.map { $0.isFinite && (0...1).contains($0) } ?? true)
    }
    var exposureSuggestsDarkness: Bool {
        guard let iso, let exposureSeconds else { return false }
        return iso >= 600 && exposureSeconds >= 1.0 / 90.0
    }
}

/// Slow, bounded switching with a neutral brightness band and six-second
/// minimum dwell. A stale sample cannot complete a pending transition.
public struct DetectorLightingSelector {
    public static let sampleInterval: TimeInterval = 0.5
    public static let confirmationSeconds: TimeInterval = 2.5
    public static let minimumDwellSeconds: TimeInterval = 6
    public private(set) var mode: DetectorLightingMode = .automatic
    public private(set) var profile: DetectorLightingProfile = .day
    public private(set) var smoothedLuma: Double?
    public private(set) var lastSample: DetectorLightingSample?
    private var lastSampleAt: TimeInterval?
    private var lastChangeAt: TimeInterval?
    private var pending: DetectorLightingProfile?
    private var pendingSince: TimeInterval?
    private var pendingCount = 0

    public init() {}
    public func shouldSample(at time: TimeInterval) -> Bool {
        guard time.isFinite else { return false }
        return lastSampleAt.map { time < $0 || time - $0 >= Self.sampleInterval } ?? true
    }
    public mutating func setMode(_ mode: DetectorLightingMode) {
        self.mode = mode
        cancelPending()
        lastChangeAt = nil
        switch mode {
        case .automatic: break
        case .day: profile = .day
        case .night: profile = .night
        }
    }
    public mutating func resetSamples() {
        lastSampleAt = nil; lastChangeAt = nil
        smoothedLuma = nil; lastSample = nil
        cancelPending()
    }
    @discardableResult
    public mutating func update(_ sample: DetectorLightingSample, at time: TimeInterval) -> DetectorLightingProfile {
        guard time.isFinite, sample.isUsable else { cancelPending(); return profile }
        if let previous = lastSampleAt {
            if time == previous { return profile }
            if time < previous || time - previous > 1.5 { resetSamples() }
            else if time - previous < Self.sampleInterval { return profile }
        }
        lastSampleAt = time; lastSample = sample
        smoothedLuma = smoothedLuma.map { 0.75 * $0 + 0.25 * sample.medianLuma } ?? sample.medianLuma
        guard mode == .automatic, let luma = smoothedLuma else { return profile }
        let dark = (luma <= 0.20 && sample.darkFraction >= 0.50)
            || (sample.exposureSuggestsDarkness && sample.darkFraction >= 0.25)
        let bright = luma >= 0.32 && sample.darkFraction <= 0.35 && !sample.exposureSuggestsDarkness
        let desired: DetectorLightingProfile? = dark ? .night : bright ? .day : nil
        guard let desired, desired != profile else { cancelPending(); return profile }
        if pending != desired {
            pending = desired; pendingSince = time; pendingCount = 1
        } else { pendingCount += 1 }
        guard let since = pendingSince, pendingCount >= 4,
              time - since >= Self.confirmationSeconds,
              lastChangeAt.map({ time - $0 >= Self.minimumDwellSeconds }) ?? true else { return profile }
        profile = desired; lastChangeAt = time; cancelPending()
        return profile
    }
    private mutating func cancelPending() {
        pending = nil; pendingSince = nil; pendingCount = 0
    }
}
