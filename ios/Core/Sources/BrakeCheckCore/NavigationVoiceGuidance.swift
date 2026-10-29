import Foundation

public enum NavigationVoiceGuidance {
    /// Empty provider instructions still span road. When skipping them, use
    /// the distance to the spoken maneuver rather than the nearer silent step.
    public static func distanceToManeuver(projectedStep: Int, instructionStep: Int,
                                          distanceToStep: Double, stepDistances: [Double],
                                          routeDistance: Double) -> Double? {
        guard projectedStep >= 0, instructionStep >= projectedStep,
              distanceToStep.isFinite, distanceToStep >= 0,
              routeDistance.isFinite, routeDistance > 0,
              stepDistances.allSatisfy({ $0.isFinite && $0 >= 0 }) else { return nil }
        if projectedStep == instructionStep { return distanceToStep }
        let total = stepDistances.reduce(0, +)
        guard total.isFinite, total > 0, instructionStep < stepDistances.count else { return nil }
        let skipped = stepDistances[max(1, projectedStep)..<instructionStep].reduce(0, +)
        return min(routeDistance, distanceToStep + skipped / total * routeDistance)
    }

    /// Distance reminders allow more speaking/preparation time at road speed.
    /// The immediate command remains limited to the last 15 meters.
    public static func stage(distance: Double, speed: Double?) -> Int? {
        guard distance.isFinite, distance >= 0 else { return nil }
        let velocity = speed.flatMap { $0.isFinite && (0...65).contains($0) ? $0 : nil } ?? 0
        if distance <= 15 { return 3 }
        if distance <= max(40, min(200, velocity * 7)) { return 2 }
        if distance <= max(120, min(600, velocity * 15)) { return 1 }
        return 0
    }
}

/// An interrupted prompt remains eligible; only heard-to-completion stages
/// suppress later reminders. The caller always recomputes the current maneuver.
public struct NavigationVoiceProgress {
    private struct Prompt {
        let id: UUID
        let step: Int
        let stage: Int
    }
    private var completed: [Int: Int] = [:]
    private var active: Prompt?
    public init() {}

    public func shouldAnnounce(step: Int, stage: Int) -> Bool {
        guard step >= 0, (0...3).contains(stage) else { return false }
        let ongoing = active.flatMap { $0.step == step ? $0.stage : nil } ?? -1
        return stage > max(completed[step] ?? -1, ongoing)
    }

    @discardableResult
    public mutating func begin(step: Int, stage: Int) -> UUID? {
        guard shouldAnnounce(step: step, stage: stage) else { return nil }
        let id = UUID()
        active = Prompt(id: id, step: step, stage: stage)
        return id
    }

    public mutating func finish(_ id: UUID) {
        guard let active, active.id == id else { return }
        completed[active.step] = max(completed[active.step] ?? -1, active.stage)
        self.active = nil
    }

    public mutating func cancel() { active = nil }
    public mutating func reset() { completed = [:]; active = nil }
}
