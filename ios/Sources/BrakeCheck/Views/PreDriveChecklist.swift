import AVFoundation
import CoreLocation
import SwiftUI
import UIKit

/// "Before you go" (design pick #4), shown when Start trip is tapped. The phone checks camera,
/// location and volume itself; the mount needs a tap. It never blocks: Start trip always works.
struct PreDriveChecklist: View {
    @Binding var skip: Bool
    var start: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var mounted = false
    @State private var volumeConfirmed = false
    @State private var volume = AVAudioSession.sharedInstance().outputVolume
    @State private var camera = AVCaptureDevice.authorizationStatus(for: .video)
    @State private var locationManager = CLLocationManager()
    @State private var location = CLAuthorizationStatus.notDetermined
    private let poll = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    private struct Item: Identifiable {
        let id: Int
        let title: String
        let detail: String
        let done: Bool
        let icon: String
        let action: () -> Void
    }

    private var items: [Item] {
        let cameraAllowed = camera == .authorized
        let locationAllowed = location == .authorizedWhenInUse || location == .authorizedAlways
        let loud = volumeConfirmed || volume >= 0.5
        return [
            Item(id: 1, title: "Phone mounted", detail: mounted ? "In its mount, facing the road" : "Tap once the phone is in its mount",
                 done: mounted, icon: "iphone.gen3", action: { mounted.toggle() }),
            Item(id: 2, title: "Camera allowed",
                 detail: cameraAllowed ? "Checked automatically" : camera == .notDetermined ? "Tap to allow, or BrakeCheck asks when the trip starts" : "Turned off · tap to open Settings",
                 done: cameraAllowed, icon: "camera", action: {
                     if camera == .notDetermined {
                         AVCaptureDevice.requestAccess(for: .video) { _ in
                             DispatchQueue.main.async { camera = AVCaptureDevice.authorizationStatus(for: .video) }
                         }
                     } else if !cameraAllowed { openSettings() }
                 }),
            Item(id: 3, title: "Volume up", detail: loud ? "Loud enough to hear brake alerts" : "Turn it up: brake alerts are spoken out loud",
                 done: loud, icon: "speaker.wave.3", action: { volumeConfirmed.toggle() }),
            Item(id: 4, title: "Location allowed",
                 detail: locationAllowed ? "Checked automatically · speed comes from GPS" : location == .notDetermined ? "BrakeCheck asks when the trip starts" : "Turned off · tap to open Settings",
                 done: locationAllowed, icon: "location", action: {
                     if location == .denied || location == .restricted { openSettings() }
                 })
        ]
    }

    var body: some View {
        let list = items
        let done = list.filter(\.done).count
        let next = list.first { !$0.done }?.id
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Before you go").font(.brake(size: 28, weight: .bold))
                        Text("\(done) of \(list.count) done · takes 10 seconds").font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.brake(size: 15, weight: .bold))
                            .foregroundStyle(BrakeTheme.muted)
                            .frame(width: 36, height: 36)
                            .background(BrakeTheme.panel, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Close")
                }
                ProgressView(value: Double(done), total: Double(list.count))
                    .tint(BrakeTheme.accent)
                    .accessibilityLabel("\(done) of \(list.count) checks done")
                VStack(spacing: 10) {
                    ForEach(list) { item in
                        Button(action: item.action) {
                            HStack(spacing: 14) {
                                ZStack {
                                    Circle().fill(item.done ? BrakeTheme.accent : .clear)
                                    Circle().strokeBorder(BrakeTheme.accent, lineWidth: 2)
                                    if item.done {
                                        Image(systemName: "checkmark").font(.brake(size: 14, weight: .bold)).foregroundStyle(BrakeTheme.background)
                                    } else {
                                        Text("\(item.id)").font(.brake(size: 14, weight: .bold)).foregroundStyle(BrakeTheme.accentText)
                                    }
                                }
                                .frame(width: 32, height: 32)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.title).font(.brake(size: 16, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                                    Text(item.detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                Spacer(minLength: 0)
                                Image(systemName: item.icon).font(.brake(size: 18, weight: .medium)).foregroundStyle(BrakeTheme.accentText)
                            }
                            .padding(.horizontal, 16).padding(.vertical, 13)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .strokeBorder(item.id == next ? BrakeTheme.accent : .clear, lineWidth: 2))
                        }
                        .buttonStyle(.plain)
                        .accessibilityValue(item.done ? "Done" : "Not done")
                    }
                }
                Button(action: start) {
                    Label("Start trip", systemImage: "play.fill")
                        .font(.brake(size: 18, weight: .bold))
                        .foregroundStyle(BrakeTheme.background)
                        .frame(maxWidth: .infinity, minHeight: 60)
                        .background(BrakeTheme.accent, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                        .shadow(color: BrakeTheme.buttonShadow, radius: 12, y: 6)
                }
                .buttonStyle(.plain)
                .padding(.top, 4)
                Toggle("Skip the checklist next time", isOn: $skip)
                    .font(.brake(.subheadline))
                    .foregroundStyle(BrakeTheme.muted)
                    .tint(BrakeTheme.accent)
                Text("You can turn it back on in Settings › Safety.").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            .padding(20)
        }
        .background(BrakeTheme.background.ignoresSafeArea())
        .onAppear(perform: refresh)
        .onReceive(poll) { _ in refresh() }
    }

    private func refresh() {
        volume = AVAudioSession.sharedInstance().outputVolume
        camera = AVCaptureDevice.authorizationStatus(for: .video)
        location = locationManager.authorizationStatus
    }

    private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
    }
}
