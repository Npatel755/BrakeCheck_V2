import BrakeCheckCore
import AVKit
import SwiftUI
import AVFoundation
import UIKit

struct IncidentListView: View {
    @ObservedObject var model: DriveModel
    @Environment(\.dismiss) private var dismiss
    @State private var deleting: IncidentManifest?

    var body: some View {
        Group {
            List {
                if model.incidents.isEmpty {
                    ContentUnavailableView("No saved incidents", systemImage: "film.stack",
                                           description: Text("Mark an incident during a trip to preserve available footage."))
                }
                ForEach(model.incidents) { incident in
                    NavigationLink {
                        IncidentDetailView(model: model, id: incident.id)
                    } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(incident.triggerTime, style: .date).font(.brake(.headline))
                            Text(incident.triggerTime, style: .time)
                            Text(incident.reason).font(.brake(.caption))
                            Text("\(incident.segments.count) clips · \(incident.state)")
                                .font(.brake(.caption2)).foregroundStyle(.secondary)
                        }
                    }
                    .swipeActions {
                        Button("Delete", role: .destructive) { deleting = incident }
                            .disabled(incident.state == "Collecting available footage")
                    }
                }
            }
            .navigationTitle("Incidents")
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
    @State private var playing: VideoSegment?

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
                        .font(.brake(.caption))
                }
                if !incident.notes.isEmpty {
                    Section("Recording notes") {
                        ForEach(Array(incident.notes.enumerated()), id: \.offset) { _, note in Text(note).font(.brake(.caption)) }
                    }
                }
                Section("Saved clips") {
                    ForEach(incident.segments, id: \.filename) { segment in
                        HStack {
                            Button {
                                playing = segment
                            } label: {
                                Label(segment.start.formatted(date: .omitted, time: .standard), systemImage: "play.circle")
                            }
                            Spacer()
                            ShareLink(item: directory.appendingPathComponent(segment.filename)) {
                                Image(systemName: "square.and.arrow.up")
                            }
                        }
                    }
                    ShareLink(item: directory.appendingPathComponent("manifest.json")) {
                        Label("Share event metadata", systemImage: "doc.text")
                    }
                }
            }
        }
        .navigationTitle("Incident footage")
        .sheet(isPresented: Binding(get: { playing != nil }, set: { if !$0 { playing = nil } })) {
            if let playing { ClipPlayer(url: directory.appendingPathComponent(playing.filename)) }
        }
    }
}

private struct ClipPlayer: View {
    @State private var player: AVPlayer

    init(url: URL) {
        _player = State(initialValue: AVPlayer(url: url))
    }

    var body: some View {
        PlayerView(player: player)
            .onAppear {
                player.play()
            }
            .onDisappear {
                player.pause()
            }
            .ignoresSafeArea()
    }
}

private struct PlayerView: UIViewRepresentable {
    let player: AVPlayer

    func makeUIView(context: Context) -> PlayerUIView {
        let view = PlayerUIView()
        view.playerLayer.player = player
        return view
    }

    func updateUIView(_ uiView: PlayerUIView, context: Context) {
        uiView.playerLayer.player = player
    }
}

private final class PlayerUIView: UIView {

    override static var layerClass: AnyClass {
        AVPlayerLayer.self
    }

    var playerLayer: AVPlayerLayer {
        layer as! AVPlayerLayer
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        playerLayer.videoGravity = .resizeAspect
        backgroundColor = .black
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        playerLayer.videoGravity = .resizeAspect
        backgroundColor = .black
    }
}
