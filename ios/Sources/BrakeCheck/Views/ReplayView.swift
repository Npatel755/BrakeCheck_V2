import BrakeCheckCore
import SwiftUI
import UniformTypeIdentifiers

/// Chooses a recorded drive and its speed source, then runs it through the live pipeline.
/// This is the judging-table demo: GPS does not work indoors, so speed comes from the drive's log.
struct ReplayView: View {
    @ObservedObject var model: DriveModel
    @Environment(\.dismiss) private var dismiss
    @AppStorage("replay.assumedMPH") private var assumedMPH = 30.0
    @AppStorage("replay.offset") private var offset = 0.0
    @AppStorage("replay.leadSeconds") private var leadSeconds = 45.0
    @State private var video: VideoChoice?
    @State private var speedChoice: SpeedChoice = .automatic
    @State private var importedTrack: SpeedTrack?
    @State private var importKind: ImportKind = .video
    @State private var showImporter = false
    @State private var error: String?
    @State private var importJob: Task<ImportedFile, Error>?
    @State private var importID: UUID?
    @State private var replayPreparationJob: Task<SpeedTrack, Error>?
    @State private var replayPreparationID: UUID?

    enum VideoChoice: Equatable { case incident(UUID), file(URL) }
    enum SpeedChoice: Hashable { case videoOnly, automatic, saved(URL), imported, assumed }
    enum ImportKind: Sendable { case video, speedLog }

    private struct ImportedFile {
        let url: URL
        let track: SpeedTrack?
    }

    private enum ReplayPreparationError: LocalizedError {
        case missingRecordingTime, noMatchingLog

        var errorDescription: String? {
            switch self {
            case .missingRecordingTime:
                return "This video has no BrakeSense clock time. Choose Video only, a matching speed log, or assumed speed."
            case .noMatchingLog:
                return "No saved trip log covers this recording. Import the logger file or assume a speed."
            }
        }
    }

    private var replayable: [IncidentManifest] { model.incidents.filter { !$0.segments.isEmpty } }
    private var savedLogs: [URL] { SpeedLogReader.savedLogs() }
    private var isImportedVideo: Bool { if case .file = video { return true }; return false }
    private var isBusy: Bool { importJob != nil || replayPreparationJob != nil }

    var body: some View {
        Group {
            Form {
                if model.replaying {
                    Section {
                        Label(model.replayStatus, systemImage: "play.rectangle.fill")
                        Button("Stop replay", role: .destructive) { model.stopReplay() }
                    }
                } else if model.replayStatus.hasPrefix("Could not") || model.replayStatus.hasPrefix("No video")
                            || model.replayStatus.hasPrefix("No readable") {
                    Section {
                        Label(model.replayStatus, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                    }
                }

                if importJob != nil {
                    Section {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text(importKind == .video ? "Importing video…" : "Reading speed log…")
                        }
                        Button("Cancel import") { cancelImport() }
                    }
                } else if replayPreparationJob != nil {
                    Section {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text("Matching speed log…")
                        }
                        Button("Cancel replay preparation") { cancelReplayPreparation() }
                    }
                }

                Section("1 · Recorded drive") {
                    if replayable.isEmpty {
                        Text("No saved incidents with video yet. During a drive, tap Save incident right after the moment you want to show; it keeps the footage around it.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(replayable) { incident in
                        Button {
                            video = .incident(incident.id)
                            if speedChoice == .videoOnly { speedChoice = .automatic }
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(incident.triggerTime.formatted(date: .abbreviated, time: .standard))
                                    Text("\(incident.segments.count) clips · \(incident.reason)")
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if video == .incident(incident.id) { Image(systemName: "checkmark") }
                            }
                        }
                    }
                    if case .incident = video {
                        Stepper(String(format: "Start %.0f s before the marked moment", leadSeconds),
                                value: $leadSeconds, in: 10...300, step: 15)
                    }
                    Button {
                        importKind = .video; showImporter = true
                    } label: {
                        if case .file(let url) = video {
                            Label(url.lastPathComponent, systemImage: "checkmark")
                        } else {
                            Label("Import a video from Files…", systemImage: "folder")
                        }
                    }
                    Text("Save a video to Files on this iPhone before importing it. A video still on your computer will not appear here.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .disabled(isBusy)

                Section("2 · Speed during that drive") {
                    Picker("Speed source", selection: $speedChoice) {
                        Text("Video only · no speed").tag(SpeedChoice.videoOnly)
                        if !isImportedVideo {
                            Text("Match a saved trip log automatically").tag(SpeedChoice.automatic)
                        }
                        ForEach(savedLogs, id: \.self) { url in
                            Text(url.deletingPathExtension().lastPathComponent).tag(SpeedChoice.saved(url))
                        }
                        if let importedTrack {
                            Text("Imported · \(importedTrack.name)").tag(SpeedChoice.imported)
                        }
                        Text("Assume a constant speed").tag(SpeedChoice.assumed)
                    }
                    .pickerStyle(.inline)
                    .labelsHidden()

                    Button {
                        importKind = .speedLog; showImporter = true
                    } label: {
                        Label("Import GPX or CSV from a GPS logger app…", systemImage: "square.and.arrow.down")
                    }

                    if speedChoice == .videoOnly {
                        Text("Replay shows vehicle, lane, and road detections. Camera route AR needs synchronized GPS and route data and is available during live navigation only.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else if speedChoice == .assumed {
                        Stepper(String(format: "%.0f mph", assumedMPH), value: $assumedMPH, in: 5...80, step: 5)
                        Text("Lead-car speed and deceleration will be estimates built on this assumption, and the screen says so.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Stepper(String(format: "Speed log offset %+.1f s", offset), value: $offset, in: -30...30, step: 0.5)
                        Text("BrakeSense logs speed on every trip and matches it to its own recordings by clock time. Use the offset only for a log from another app or phone.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .disabled(isBusy)

                Section {
                    Button("Start replay") { start() }
                        .disabled(video == nil || model.replaying || model.tripActive || model.demo || isBusy)
                    if let error {
                        Text(error).font(.caption).foregroundStyle(.orange)
                    }
                    if model.tripActive || model.demo {
                        Text("End the trip and turn off simulated metrics first.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Replay a drive")
            .navigationBarTitleDisplayMode(.inline)
        }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: allowedTypes) { result in
            handleImport(result, kind: importKind)
        }
        .onDisappear {
            cancelImport()
            cancelReplayPreparation()
        }
        .tint(BrakeTheme.accent)
    }

    private var allowedTypes: [UTType] {
        switch importKind {
        case .video: return [.movie, .mpeg4Movie, .quickTimeMovie]
        case .speedLog:
            return [.commaSeparatedText, .plainText, .xml] + [UTType(filenameExtension: "gpx")].compactMap { $0 }
        }
    }

    private func handleImport(_ result: Result<URL, Error>, kind: ImportKind) {
        guard !isBusy else { return }
        let source: URL
        switch result {
        case .success(let url): source = url
        case .failure(let failure):
            let cocoa = failure as NSError
            if cocoa.domain != NSCocoaErrorDomain || cocoa.code != NSUserCancelledError {
                error = failure.localizedDescription
            }
            return
        }
        error = nil
        let id = UUID()
        importID = id
        let job = Task.detached(priority: .userInitiated) {
            try Self.importFile(source, kind: kind)
        }
        importJob = job
        Task { @MainActor in
            do {
                let imported = try await job.value
                guard importID == id else {
                    try? FileManager.default.removeItem(at: imported.url.deletingLastPathComponent())
                    return
                }
                importID = nil
                importJob = nil
                switch kind {
                case .video:
                    video = .file(imported.url)
                    if speedChoice == .automatic { speedChoice = .videoOnly }
                case .speedLog:
                    importedTrack = imported.track
                    speedChoice = .imported
                }
            } catch {
                guard importID == id else { return }
                importID = nil
                importJob = nil
                if !(error is CancellationError) { self.error = error.localizedDescription }
            }
        }
    }

    private func cancelImport() {
        importID = nil
        importJob?.cancel()
        importJob = nil
    }

    private static func importFile(_ url: URL, kind: ImportKind) throws -> ImportedFile {
        let local = try copyIn(url)
        do {
            try Task.checkCancellation()
            let track = kind == .speedLog ? try SpeedLogReader.load(local) : nil
            try Task.checkCancellation()
            return ImportedFile(url: local, track: track)
        } catch {
            try? FileManager.default.removeItem(at: local.deletingLastPathComponent())
            throw error
        }
    }

    private static func copyIn(_ url: URL) throws -> URL {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ReplayImports", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(url.lastPathComponent)
        do {
            try FileManager.default.copyItem(at: url, to: destination)
        } catch {
            try? FileManager.default.removeItem(at: directory)
            throw error
        }
        return destination
    }

    private func clips() -> [ReplayService.Clip] {
        switch video {
        case .file(let url):
            return [ReplayService.Clip(url: url, start: nil)]
        case .incident(let id):
            guard let incident = model.incidents.first(where: { $0.id == id }) else { return [] }
            let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Incidents").appendingPathComponent(id.uuidString)
            let from = incident.triggerTime.addingTimeInterval(-leadSeconds)
            return incident.segments
                .filter { $0.end >= from }
                .map { ReplayService.Clip(url: directory.appendingPathComponent($0.filename), start: $0.start) }
        case nil:
            return []
        }
    }

    private func start() {
        guard !isBusy else { return }
        let selected = clips()
        guard !selected.isEmpty else { error = "That recording has no clips in the chosen window."; return }
        switch speedChoice {
        case .videoOnly:
            beginReplay(selected, track: nil, assumedSpeed: nil)
        case .imported:
            guard let importedTrack else { error = "Import a speed log before starting replay."; return }
            beginReplay(selected, track: importedTrack, assumedSpeed: nil)
        case .assumed:
            beginReplay(selected, track: nil, assumedSpeed: assumedMPH * 0.44704)
        case .automatic, .saved:
            let choice = speedChoice
            let logs = choice == .automatic ? savedLogs : []
            let recordedAt = selected.first?.start
            let id = UUID()
            error = nil
            replayPreparationID = id
            let job = Task.detached(priority: .userInitiated) {
                try Self.loadReplayTrack(choice: choice, savedLogs: logs, recordedAt: recordedAt)
            }
            replayPreparationJob = job
            Task { @MainActor in
                do {
                    let track = try await job.value
                    guard replayPreparationID == id else { return }
                    replayPreparationID = nil
                    replayPreparationJob = nil
                    beginReplay(selected, track: track, assumedSpeed: nil)
                } catch {
                    guard replayPreparationID == id else { return }
                    replayPreparationID = nil
                    replayPreparationJob = nil
                    if !(error is CancellationError) { self.error = error.localizedDescription }
                }
            }
        }
    }

    private func cancelReplayPreparation() {
        replayPreparationID = nil
        replayPreparationJob?.cancel()
        replayPreparationJob = nil
    }

    private static func loadReplayTrack(choice: SpeedChoice, savedLogs: [URL], recordedAt: Date?) throws -> SpeedTrack {
        switch choice {
        case .automatic:
            guard let recordedAt else { throw ReplayPreparationError.missingRecordingTime }
            for url in savedLogs {
                try Task.checkCancellation()
                if let track = try? SpeedLogReader.load(url), track.covers(recordedAt) { return track }
            }
            throw ReplayPreparationError.noMatchingLog
        case .saved(let url):
            let track = try SpeedLogReader.load(url)
            try Task.checkCancellation()
            return track
        default:
            throw ReplayPreparationError.noMatchingLog
        }
    }

    private func beginReplay(_ clips: [ReplayService.Clip], track: SpeedTrack?, assumedSpeed: Double?) {
        guard !model.tripActive, !model.demo, !model.replaying else {
            error = "End the trip and turn off simulated metrics first."
            return
        }
        model.startReplay(clips, track: track, assumedSpeed: assumedSpeed, offset: offset)
        guard model.replaying else {
            error = "Replay could not start. Try again after the current drive ends."
            return
        }
        error = nil
        dismiss()
    }
}
