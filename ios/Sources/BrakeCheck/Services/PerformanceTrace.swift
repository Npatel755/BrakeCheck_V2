import BrakeCheckCore
import Foundation
import QuartzCore
import UIKit

/// Opt-in, bounded profiling. No files, locations, images, or model inputs are recorded.
final class PerformanceTrace: @unchecked Sendable {
    enum Stage: String, Codable, CaseIterable {
        case cameraCallback, recordingWrite, vehicleInference, laneInference, roadInference, viewpointInference
        case gpsProgress, arProjection, mapUpdate, mapRender, cameraIntervals, mainCallbackDelay
    }
    struct Token {
        fileprivate let stage: Stage
        fileprivate let generation: UInt64
        fileprivate let started: Double
    }
    struct Report: Codable, Sendable {
        let schemaVersion: Int
        let build: String
        let active: Bool
        let elapsedSeconds: Double
        let stages: [String: PerformanceStatistics]
        let display: DisplayPacingStatistics
        let thermalStates: [String: UInt64]
        let note: String
    }
    private static let shared = PerformanceTrace()
    private let lock = NSLock()
    private var active = false
    private var generation: UInt64 = 0
    private var started: Double?
    private var stopped: Double?
    private var lastCameraFrame: Double?
    private var stages = Dictionary(uniqueKeysWithValues: Stage.allCases.map { ($0, PerformanceStatistics()) })
    private var display = DisplayPacingStatistics()
    private var thermalStates: [String: UInt64] = [:]

    static func begin(_ stage: Stage) -> Token? {
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        guard owner.active else { return nil }
        return Token(stage: stage, generation: owner.generation, started: CACurrentMediaTime())
    }
    static func end(_ token: Token?) {
        guard let token else { return }
        let elapsed = CACurrentMediaTime() - token.started
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        guard owner.active, owner.generation == token.generation else { return }
        owner.stages[token.stage, default: PerformanceStatistics()].record(seconds: elapsed)
    }
    static func captureFrame(at timestamp: Double) {
        guard timestamp.isFinite else { return }
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        guard owner.active else { return }
        if let last = owner.lastCameraFrame, timestamp > last {
            owner.stages[.cameraIntervals, default: PerformanceStatistics()].record(seconds: timestamp - last)
        }
        owner.lastCameraFrame = timestamp
    }
    fileprivate static func startSession() {
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        owner.generation &+= 1
        owner.active = true; owner.started = CACurrentMediaTime(); owner.stopped = nil
        owner.lastCameraFrame = nil; owner.display = DisplayPacingStatistics()
        owner.stages = Dictionary(uniqueKeysWithValues: Stage.allCases.map { ($0, PerformanceStatistics()) })
        owner.thermalStates = [:]
    }
    fileprivate static func stopSession() {
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        owner.stopped = CACurrentMediaTime(); owner.active = false
    }
    fileprivate static func resetContinuity() {
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        owner.display.resetContinuity(); owner.lastCameraFrame = nil
    }
    fileprivate static func displayFrame(_ link: CADisplayLink) {
        let delay = max(0, CACurrentMediaTime() - link.timestamp)
        let thermal = String(describing: ProcessInfo.processInfo.thermalState)
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        guard owner.active else { return }
        owner.display.record(timestamp: link.timestamp, targetInterval: link.targetTimestamp - link.timestamp)
        owner.stages[.mainCallbackDelay, default: PerformanceStatistics()].record(seconds: delay)
        owner.thermalStates[thermal, default: 0] += 1
    }
    static func snapshot() -> Report {
        let owner = shared
        owner.lock.lock(); defer { owner.lock.unlock() }
        let elapsed = owner.started.map { max(0, (owner.stopped ?? CACurrentMediaTime()) - $0) } ?? 0
        return Report(schemaVersion: 1,
            build: Bundle.main.object(forInfoDictionaryKey: "BrakeCheckMeasurementRevision") as? String ?? "unknown",
            active: owner.active, elapsedSeconds: elapsed,
            stages: Dictionary(uniqueKeysWithValues: owner.stages.map { ($0.key.rawValue, $0.value) }),
            display: owner.display, thermalStates: owner.thermalStates,
            note: "Opt-in bounded histograms; percentile values are bucket upper bounds. Display callbacks measure scheduling, not guaranteed rendered FPS. Recording work and thermal load can affect results.")
    }
}

/// Runs only while the driver explicitly records a performance trace.
@MainActor
final class PerformanceRecordingController: NSObject {
    static let shared = PerformanceRecordingController()
    private var link: CADisplayLink?
    override private init() {
        super.init()
        NotificationCenter.default.addObserver(self, selector: #selector(background),
            name: UIApplication.willResignActiveNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(foreground),
            name: UIApplication.didBecomeActiveNotification, object: nil)
    }
    func start() {
        stop()
        PerformanceTrace.startSession()
        let next = CADisplayLink(target: self, selector: #selector(tick(_:)))
        // Do not request a higher display refresh rate just to measure the app.
        next.add(to: .main, forMode: .common)
        next.isPaused = UIApplication.shared.applicationState != .active
        link = next
    }
    func stop() {
        link?.invalidate(); link = nil
        PerformanceTrace.stopSession()
    }
    @objc private func tick(_ link: CADisplayLink) { PerformanceTrace.displayFrame(link) }
    @objc private func background() { link?.isPaused = true; PerformanceTrace.resetContinuity() }
    @objc private func foreground() { PerformanceTrace.resetContinuity(); link?.isPaused = false }
}
