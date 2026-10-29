import BrakeCheckCore
import SwiftUI

struct PerformanceDiagnosticsPanel: View {
    @State private var report = PerformanceTrace.snapshot()
    @State private var exportURL: URL?
    @State private var exportError: String?
    @State private var preparing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Record while using Camera and Navigate, then return here to inspect the results.")
                .font(.brake(.caption)).foregroundStyle(.secondary)
            HStack {
                Button(report.active ? "Stop performance recording" : "Record performance") {
                    if report.active { PerformanceRecordingController.shared.stop() }
                    else { PerformanceRecordingController.shared.start(); exportURL = nil }
                    report = PerformanceTrace.snapshot()
                }
                .buttonStyle(.bordered)
            }
            Text(report.active ? "Recording performance" : "Performance recording off")
                .font(.brake(.caption, weight: .semibold))
            LabeledContent("Measured time", value: String(format: "%.0f seconds", report.elapsedSeconds))
            LabeledContent("Delayed display callbacks", value: "\(report.display.delayedCallbacks)")
            LabeledContent("Estimated missed refreshes", value: "\(report.display.estimatedMissedRefreshes)")
            ForEach(PerformanceTrace.Stage.allCases, id: \.rawValue) { stage in
                if let stats = report.stages[stage.rawValue], stats.count > 0 {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(stage.rawValue).font(.brake(.caption, weight: .semibold))
                        Text(String(format: "%llu samples | mean %.1f ms | p95 <= %.1f ms | max %.1f ms",
                            stats.count, stats.meanSeconds * 1000,
                            (stats.percentileUpperBound(0.95) ?? 0) * 1000, stats.maximumSeconds * 1000))
                            .font(.brake(.caption2)).foregroundStyle(.secondary)
                    }
                }
            }
            Button(preparing ? "Preparing report..." : "Prepare performance report") { prepareExport() }
                .disabled(preparing || report.elapsedSeconds <= 0)
            if let exportURL { ShareLink("Share performance report", item: exportURL) }
            if let exportError { Text(exportError).font(.brake(.caption)).foregroundStyle(BrakeTheme.warning) }
            Text("No images or GPS coordinates are captured. These measurements do not prove rendered FPS; use an Instruments trace for frame-rendering diagnosis.")
                .font(.brake(.caption2)).foregroundStyle(.secondary)
        }
        .task {
            while !Task.isCancelled {
                report = PerformanceTrace.snapshot()
                do { try await Task.sleep(for: .seconds(1)) } catch { return }
            }
        }
    }

    private func prepareExport() {
        preparing = true; exportError = nil
        let snapshot = PerformanceTrace.snapshot()
        Task {
            do {
                let url = try await Task.detached(priority: .utility) {
                    let encoder = JSONEncoder()
                    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
                    let data = try encoder.encode(snapshot)
                    let destination = FileManager.default.temporaryDirectory
                        .appendingPathComponent("BrakeSense-performance.json")
                    try data.write(to: destination, options: .atomic)
                    return destination
                }.value
                exportURL = url
            } catch { exportError = error.localizedDescription }
            preparing = false
        }
    }
}
