import BrakeCheckCore
import SwiftUI
import UniformTypeIdentifiers

/// Chooses a recorded drive and its speed source, then runs it through the live pipeline.
/// This is the judging-table demo: GPS does not work indoors, so speed comes from the drive's log.
struct ReplayView: View {
    let model: DriveModel
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
    @State private var importing = false
    @State private var importTask: Task<Void, Never>?
    @State private var importGeneration = UUID()

    enum VideoChoice: Equatable { case incident(UUID), file(URL) }
    enum SpeedChoice: Hashable { case automatic, saved(URL), imported, assumed }
    enum ImportKind: Sendable, Equatable { case video, speedLog }

    private var replayable: [IncidentManifest] { model.incidents.filter { !$0.segments.isEmpty } }
    private var savedLogs: [URL] { SpeedLogReader.savedLogs() }

    var body: some View {
        Group {
            Form {
                if model.replaying {
                    Section {
                        Label(model.replayStatus, systemImage: "play.rectangle.fill")
                        Button("Stop replay", role: .destructive) { model.stopReplay() }
                    }
                }

                Section("1 · Recorded drive") {
                    if replayable.isEmpty {
                        Text("No saved incidents with video yet. During a drive, tap Save incident right after the moment you want to show; it keeps the footage around it.")
                            .font(.brake(.caption)).foregroundStyle(.secondary)
                    }
                    ForEach(replayable) { incident in
                        Button { video = .incident(incident.id) } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(incident.triggerTime.formatted(date: .abbreviated, time: .standard))
                                    Text("\(incident.segments.count) clips · \(incident.reason)")
                                        .font(.brake(.caption2)).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if video == .incident(incident.id) { Image(systemName: "checkmark") }
                            }
                        }
                        .disabled(importing)
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
                    .disabled(importing)
                }

                Section("2 · Speed during that drive") {
                    Picker("Speed source", selection: $speedChoice) {
                        Text("Match a saved trip log automatically").tag(SpeedChoice.automatic)
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
                    .disabled(importing)

                    Button {
                        importKind = .speedLog; showImporter = true
                    } label: {
                        Label("Import GPX or CSV from a GPS logger app…", systemImage: "square.and.arrow.down")
                    }
                    .disabled(importing)

                    if speedChoice == .assumed {
                        Stepper(String(format: "%.0f mph", assumedMPH), value: $assumedMPH, in: 5...80, step: 5)
                        Text("Lead-car speed and deceleration will be estimates built on this assumption, and the screen says so.")
                            .font(.brake(.caption)).foregroundStyle(.secondary)
                    } else {
                        Stepper(String(format: "Speed log offset %+.1f s", offset), value: $offset, in: -30...30, step: 0.5)
                        Text("BrakeSense logs speed on every trip and matches it to its own recordings by clock time. Use the offset only for a log from another app or phone.")
                            .font(.brake(.caption)).foregroundStyle(.secondary)
                    }
                }

                Section {
                    if importing { ProgressView("Importing file…") }
                    Button("Start replay") { start() }
                        .disabled(importing || video == nil || model.replaying || model.tripActive || model.demo)
                    if let error {
                        Text(error).font(.brake(.caption)).foregroundStyle(.orange)
                    }
                    if model.tripActive || model.demo {
                        Text("End the trip and turn off simulated metrics first.")
                            .font(.brake(.caption)).foregroundStyle(.secondary)
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
            importGeneration = UUID()
            importTask?.cancel()
            importing = false
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
        guard case .success(let source) = result else {
            if case .failure(let failure) = result { error = failure.localizedDescription }
            return
        }
        importTask?.cancel()
        let generation = UUID()
        importGeneration = generation
        importing = true
        error = nil
        importTask = Task { @MainActor in
            defer { if importGeneration == generation { importing = false } }
            do {
                let (local, track) = try await Task.detached(priority: .userInitiated) {
                    let access = source.startAccessingSecurityScopedResource()
                    defer { if access { source.stopAccessingSecurityScopedResource() } }
                    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ReplayImports", isDirectory: true)
                    let local = try ReplayImportStore.copy(source, to: directory)
                    do {
                        return (local, kind == .speedLog ? try SpeedLogReader.load(local) : nil)
                    } catch {
                        try? FileManager.default.removeItem(at: local.deletingLastPathComponent())
                        throw error
                    }
                }.value
                guard !Task.isCancelled, importGeneration == generation else {
                    try? FileManager.default.removeItem(at: local.deletingLastPathComponent())
                    return
                }
                switch kind {
                case .video: video = .file(local)
                case .speedLog:
                    importedTrack = track
                    speedChoice = .imported
                }
            } catch {
                guard !Task.isCancelled, importGeneration == generation else { return }
                self.error = error.localizedDescription
            }
        }
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
        let selected = clips()
        guard !selected.isEmpty else { error = "That recording has no clips in the chosen window."; return }
        do {
            var track: SpeedTrack?
            var assumed: Double?
            switch speedChoice {
            case .automatic:
                guard let recordedAt = selected.first?.start else {
                    error = "An imported video has no BrakeCheck clock time. Pick its speed log or an assumed speed."
                    return
                }
                track = savedLogs.lazy.compactMap { try? SpeedLogReader.load($0) }
                    .first(where: { $0.covers(recordedAt.addingTimeInterval(offset)) })
                guard track != nil else {
                    error = "No saved trip log covers this recording. Import the logger file or assume a speed."
                    return
                }
            case .saved(let url):
                track = try SpeedLogReader.load(url)
            case .imported:
                track = importedTrack
            case .assumed:
                assumed = assumedMPH * 0.44704
            }
            error = nil
            model.startReplay(selected, track: track, assumedSpeed: assumed, offset: offset)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
