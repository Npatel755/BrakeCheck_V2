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

    enum VideoChoice: Equatable { case incident(UUID), file(URL) }
    enum SpeedChoice: Hashable { case automatic, saved(URL), imported, assumed }
    enum ImportKind { case video, speedLog }

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
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(replayable) { incident in
                        Button { video = .incident(incident.id) } label: {
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

                    Button {
                        importKind = .speedLog; showImporter = true
                    } label: {
                        Label("Import GPX or CSV from a GPS logger app…", systemImage: "square.and.arrow.down")
                    }

                    if speedChoice == .assumed {
                        Stepper(String(format: "%.0f mph", assumedMPH), value: $assumedMPH, in: 5...80, step: 5)
                        Text("Lead-car speed and deceleration will be estimates built on this assumption, and the screen says so.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Stepper(String(format: "Speed log offset %+.1f s", offset), value: $offset, in: -30...30, step: 0.5)
                        Text("BrakeCheck logs speed on every trip and matches it to its own recordings by clock time. Use the offset only for a log from another app or phone.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                Section {
                    Button("Start replay") { start() }
                        .disabled(video == nil || model.replaying || model.tripActive || model.demo)
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
        do {
            let local = try copyIn(try result.get())
            switch kind {
            case .video:
                video = .file(local)
            case .speedLog:
                importedTrack = try SpeedLogReader.load(local)
                speedChoice = .imported
            }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func copyIn(_ url: URL) throws -> URL {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ReplayImports", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(url.lastPathComponent)
        try? FileManager.default.removeItem(at: destination)
        try FileManager.default.copyItem(at: url, to: destination)
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
                track = savedLogs.lazy.compactMap { try? SpeedLogReader.load($0) }.first(where: { $0.covers(recordedAt) })
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
