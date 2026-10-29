import BrakeCheckCore
import Observation
import SwiftUI

/// Isolated presentation updates: an AR frame must not invalidate Home,
/// navigation's map, and the entire driving dashboard twice per capture.
@MainActor
@Observable
final class CameraARPresentation {
    private(set) var scene: CameraARScene?
    private(set) var calibrationEstimate: CameraCalibrationEstimate?
    private(set) var calibrationProgress = 0
    private(set) var alignmentLocked = false
    private(set) var mountConfirmed = false
    private(set) var level: CameraARConfidence = .lost
    private(set) var guidanceDetail = "Waiting for road tracking"
    @ObservationIgnored private var maskIdentity: UUID?
    @ObservationIgnored private var maskPath = Path()
    @ObservationIgnored private var diagnosticMaskIdentity: UUID?
    @ObservationIgnored private var diagnosticMaskPath = Path()
    @ObservationIgnored private var statusPresentation = ARStatusPresentation()
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
        update(scene: nil, estimate: nil); setLevel(.lost); maskIdentity = nil; maskPath = Path()
        diagnosticMaskIdentity = nil; diagnosticMaskPath = Path()
        statusPresentation = ARStatusPresentation(); guidanceDetail = "Waiting for road tracking"
    }
    func visibilityPath() -> Path {
        guard let mask = scene?.road?.visibility else { return Path() }
        if maskIdentity == mask.renderIdentity { return maskPath }
        maskPath = Self.path(mask.visibilitySpans, width: mask.width, height: mask.height)
        maskIdentity = mask.renderIdentity
        return maskPath
    }
    /// Raw model output, independent of the scanner's accepted road region.
    func diagnosticRoadPath() -> Path {
        guard let mask = scene?.diagnosticRoadMask else { return Path() }
        if diagnosticMaskIdentity == mask.renderIdentity { return diagnosticMaskPath }
        diagnosticMaskPath = Self.path(mask.rawSpans, width: mask.width, height: mask.height)
        diagnosticMaskIdentity = mask.renderIdentity
        return diagnosticMaskPath
    }
    private static func path(_ spans: [RoadVisibilitySpan], width: Int, height: Int) -> Path {
        var path = Path()
        for span in spans {
            path.addRect(CGRect(x: Double(span.firstColumn) / Double(width), y: Double(span.row) / Double(height),
                width: Double(span.endColumn - span.firstColumn) / Double(width), height: 1.0 / Double(height)))
        }
        return path
    }
}
