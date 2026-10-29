import BrakeCheckCore
import AVKit
import SwiftUI
import AVFoundation
import UIKit

struct IncidentListView: View {
    @ObservedObject var model: DriveModel
    @Environment(\.dismiss) private var dismiss
    @State private var deleting: IncidentManifest?
    @State private var recoveryVideo: URL?

    var body: some View {
        Group {
            List {
                if !model.recordingRecoveryWarnings.isEmpty || !model.unindexedRecordings.isEmpty {
                    Section("Recording recovery") {
                        Text("Some recording details could not be recovered. Existing files have been kept; export important clips before repairing the library.")
                            .font(.caption).foregroundStyle(.orange)
                        ForEach(Array(model.recordingRecoveryWarnings.enumerated()), id: \.offset) { _, warning in
                            Text(warning).font(.caption)
                        }
                        ForEach(model.unindexedRecordings) { clip in
                            VStack(alignment: .leading, spacing: 6) {
                                Button { recoveryVideo = clip.url } label: {
                                    Label(clip.url.lastPathComponent, systemImage: "play.circle")
                                }
                                Text(clip.detail).font(.caption).foregroundStyle(.secondary)
                                ShareLink(item: clip.url) { Label("Export clip", systemImage: "square.and.arrow.up") }
                            }
                        }
                    }
                }
                if model.incidents.isEmpty {
                    ContentUnavailableView("No saved incidents", systemImage: "film.stack",
                                           description: Text("Mark an incident during a trip to preserve available footage."))
                }
                ForEach(model.incidents) { incident in
                    NavigationLink {
                        IncidentDetailView(model: model, id: incident.id)
                    } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(incident.triggerTime, style: .date).font(.headline)
                            Text(incident.triggerTime, style: .time)
                            Text(incident.reason).font(.caption)
                            Text("\(incident.segments.count) clips · \(incident.state)")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    .swipeActions {
                        Button("Delete", role: .destructive) { deleting = incident }
                            .disabled(incident.state == "Collecting available footage")
                    }
                }
            }
            .navigationTitle("Incidents")
            .onAppear { model.camera.refreshIncidents() }
            .sheet(isPresented: Binding(get: { recoveryVideo != nil }, set: { if !$0 { recoveryVideo = nil } })) {
                if let recoveryVideo { ClipPlayer(url: recoveryVideo) }
            }
            .confirmationDialog("Delete this incident and its saved clips?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                Button("Delete incident", role: .destructive) {
                    if let deleting { model.deleteIncident(deleting.id) }
                    deleting = nil
                }
            }
        }
    }
}

struct IncidentDetailView: View {
    @ObservedObject var model: DriveModel
    let id: UUID
    @State private var playing: [URL] = []

    private var incident: IncidentManifest? { model.incidents.first { $0.id == id } }
    private var directory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Incidents").appendingPathComponent(id.uuidString)
    }

    var body: some View {
        List {
            if let incident {
                Section("Requested window") {
                    Text("\(incident.requestedStart.formatted()) → \(incident.requestedEnd.formatted())")
                    Text(incident.state).foregroundStyle(.secondary)
                    Text("Only available footage is saved. Individual clips may include up to 10 extra seconds at window boundaries. Gaps are not reconstructed.")
                        .font(.caption)
                }
                if !incident.notes.isEmpty {
                    Section("Recording notes") {
                        ForEach(Array(incident.notes.enumerated()), id: \.offset) { _, note in Text(note).font(.caption) }
                    }
                }
                Section("Saved clips") {
                    let availableClips = incident.segments.sorted { $0.start < $1.start }
                        .map { directory.appendingPathComponent($0.filename) }
                        .filter(RecordingLibraryRecovery.isRegularFile)
                    if availableClips.count > 1 {
                        Button { playing = availableClips } label: {
                            Label("Play all available clips", systemImage: "play.rectangle.on.rectangle")
                        }
                        Text("Plays available clips in time order, skipping periods with no recording.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(incident.segments, id: \.filename) { segment in
                        let available = RecordingLibraryRecovery.isRegularFile(directory.appendingPathComponent(segment.filename))
                        HStack {
                            Button {
                                playing = [directory.appendingPathComponent(segment.filename)]
                            } label: {
                                Label(segment.start.formatted(date: .omitted, time: .standard), systemImage: "play.circle")
                            }
                            .disabled(!available)
                            Spacer()
                            ShareLink(item: directory.appendingPathComponent(segment.filename)) {
                                Image(systemName: "square.and.arrow.up")
                            }
                            .disabled(!available)
                        }
                        if !available { Text("Clip unavailable. Its timing details have been kept for recovery.").font(.caption).foregroundStyle(.orange) }
                    }
                    ShareLink(item: directory.appendingPathComponent("manifest.json")) {
                        Label("Share event metadata", systemImage: "doc.text")
                    }
                }
            }
        }
        .navigationTitle("Incident footage")
        .sheet(isPresented: Binding(get: { !playing.isEmpty }, set: { if !$0 { playing = [] } })) {
            ClipPlayer(urls: playing)
        }
    }
}

private struct ClipPlayer: View {
    @State private var player: AVPlayer
    @Environment(\.dismiss) private var dismiss

    init(url: URL) {
        _player = State(initialValue: AVPlayer(url: url))
    }

    init(urls: [URL]) {
        _player = State(initialValue: AVQueuePlayer(items: urls.map { AVPlayerItem(url: $0) }))
    }

    var body: some View {
        NavigationStack {
            PlaybackController(player: player)
                .background(.black)
                .navigationTitle("Saved footage")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { player.pause(); dismiss() }
                    }
                }
                .onAppear { player.play() }
                .onDisappear { player.pause() }
        }
    }
}

private struct PlaybackController: UIViewControllerRepresentable {
    let player: AVPlayer

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        controller.player = player
        controller.showsPlaybackControls = true
        controller.videoGravity = .resizeAspect
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {
        if controller.player !== player { controller.player = player }
    }

    static func dismantleUIViewController(_ controller: AVPlayerViewController, coordinator: ()) {
        controller.player?.pause()
        controller.player = nil
    }
}
