import BrakeCheckCore
import Combine
import SwiftUI

/// Isolated presentation updates: an AR frame must not invalidate Home,
/// navigation's map, and the entire driving dashboard twice per capture.
@MainActor
final class CameraARPresentation: ObservableObject {
    @Published private(set) var scene: CameraARScene?
    @Published private(set) var calibrationEstimate: CameraCalibrationEstimate?
    @Published private(set) var calibrationProgress = 0
    @Published private(set) var alignmentLocked = false
    @Published private(set) var mountConfirmed = false
    @Published private(set) var level: CameraARConfidence = .lost
    @Published private(set) var guidanceDetail = "Waiting for road tracking"
    private var maskTime: TimeInterval?
    private var maskPath = Path()
    private var diagnosticMaskTime: TimeInterval?
    private var diagnosticMaskPath = Path()
    private var statusPresentation = ARStatusPresentation()
    func update(scene: CameraARScene?, estimate: CameraCalibrationEstimate?, calibrationProgress: Int = 0, alignmentLocked: Bool = false, mountConfirmed: Bool = false) {
        if let scene {
            if self.scene?.capturedAt != scene.capturedAt { self.scene = scene }
        } else if self.scene != nil { self.scene = nil }
        if calibrationEstimate != estimate { calibrationEstimate = estimate }
        let progress = min(8, max(0, calibrationProgress))
        if self.calibrationProgress != progress { self.calibrationProgress = progress }
        setAlignmentLocked(alignmentLocked)
        setMountConfirmed(mountConfirmed)
    }
    func setAlignmentLocked(_ locked: Bool) { if alignmentLocked != locked { alignmentLocked = locked } }
    func setMountConfirmed(_ confirmed: Bool) { if mountConfirmed != confirmed { mountConfirmed = confirmed } }
    func clearCalibration() { calibrationEstimate = nil; calibrationProgress = 0; setAlignmentLocked(false); setMountConfirmed(false) }
    func setLevel(_ level: CameraARConfidence) { if self.level != level { self.level = level } }
    func setGuidanceDetail(_ detail: String) {
        let stable = statusPresentation.update(detail, at: ProcessInfo.processInfo.systemUptime)
        if guidanceDetail != stable { guidanceDetail = stable }
    }
    func reset() {
        update(scene: nil, estimate: nil); setLevel(.lost); maskTime = nil; maskPath = Path()
        diagnosticMaskTime = nil; diagnosticMaskPath = Path()
        statusPresentation = ARStatusPresentation(); guidanceDetail = "Waiting for road tracking"
    }
    func visibilityPath() -> Path {
        guard let scene, let mask = scene.road?.visibility else { return Path() }
        if maskTime == scene.capturedAt { return maskPath }
        var path = Path()
        for row in 0..<mask.height {
            var start: Int?
            for column in 0...mask.width {
                let visible = column < mask.width && mask.contains(x: (Double(column) + 0.5) / Double(mask.width),
                    y: 1 - (Double(row) + 0.5) / Double(mask.height))
                if visible, start == nil { start = column }
                if !visible, let first = start {
                    path.addRect(CGRect(x: Double(first) / Double(mask.width), y: Double(row) / Double(mask.height),
                        width: Double(column - first) / Double(mask.width), height: 1.0 / Double(mask.height)))
                    start = nil
                }
            }
        }
        maskTime = scene.capturedAt; maskPath = path
        return path
    }
    /// Raw model output, independent of the scanner's accepted road region.
    func diagnosticRoadPath() -> Path {
        guard let scene, let mask = scene.diagnosticRoadMask else { return Path() }
        if diagnosticMaskTime == scene.roadObservedAt { return diagnosticMaskPath }
        var path = Path()
        for row in 0..<mask.height {
            var start: Int?
            for column in 0...mask.width {
                let road = column < mask.width && mask.pixels[row * mask.width + column] != 0
                if road, start == nil { start = column }
                if !road, let first = start {
                    path.addRect(CGRect(x: Double(first) / Double(mask.width),
                        y: Double(row) / Double(mask.height),
                        width: Double(column - first) / Double(mask.width),
                        height: 1.0 / Double(mask.height)))
                    start = nil
                }
            }
        }
        diagnosticMaskTime = scene.roadObservedAt; diagnosticMaskPath = path
        return path
    }
}
