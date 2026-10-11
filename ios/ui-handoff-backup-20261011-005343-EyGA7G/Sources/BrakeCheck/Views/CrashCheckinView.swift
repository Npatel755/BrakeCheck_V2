import SwiftUI

struct CrashCheckinView: View {
    @ObservedObject var service: SafetyService
    @State private var confirmClose = false
    var body: some View {
        HStack(spacing: 32) {
            VStack(spacing: 14) {
                ZStack {
                    Circle().stroke(BrakeTheme.accent.opacity(0.12), lineWidth: 8)
                    Circle().trim(from: 0, to: service.resolved ? 1 : CGFloat(min(20, service.remaining)) / 20)
                        .stroke(BrakeTheme.accent, style: StrokeStyle(lineWidth: 8, lineCap: .round)).rotationEffect(.degrees(-90))
                    if service.resolved {
                        Image(systemName: "checkmark").font(.system(size: 44, weight: .light)).foregroundStyle(BrakeTheme.accent)
                    } else {
                        Text("\(service.remaining)").font(.system(size: 54, weight: .light, design: .rounded)).monospacedDigit()
                    }
                }.frame(width: 130, height: 130)
                Text(service.isDryRun ? "DEMO · NO MESSAGES" : "EMERGENCY CHECK-IN")
                    .font(.system(size: 10, weight: .bold)).tracking(1).foregroundStyle(BrakeTheme.accent)
            }.frame(maxWidth: .infinity)
            VStack(alignment: .leading, spacing: 15) {
                Text(service.checkinTitle).font(.system(size: 30, weight: .semibold, design: .rounded))
                Text(service.checkinDetail).font(.subheadline).foregroundStyle(BrakeTheme.muted).fixedSize(horizontal: false, vertical: true)
                if service.resolved {
                    Button("Done") { service.closeCheckin() }.buttonStyle(.borderedProminent).tint(BrakeTheme.accent).foregroundStyle(BrakeTheme.background)
                } else {
                    HStack(spacing: 12) {
                        Button { service.respond(ok: true) } label: {
                            Label("I'm okay", systemImage: "checkmark").padding(.vertical, 8).frame(maxWidth: .infinity)
                        }.buttonStyle(.borderedProminent).tint(BrakeTheme.accent).foregroundStyle(BrakeTheme.background)
                        Button { service.respond(ok: false) } label: {
                            Label("Send help", systemImage: "exclamationmark.bubble").padding(.vertical, 8).frame(maxWidth: .infinity)
                        }.buttonStyle(.borderedProminent).tint(.red)
                    }.disabled(!service.canRespond)
                    if service.connectionLost {
                        Button("Close with result unknown") { confirmClose = true }.font(.caption)
                    }
                    Text(service.isDryRun ? "This rehearsal never alerts contacts." : "No response triggers a message to confirmed contacts. Emergency services are not called.")
                        .font(.caption).foregroundStyle(BrakeTheme.muted)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(BrakeTheme.background.ignoresSafeArea())
        .interactiveDismissDisabled(!service.resolved)
        .confirmationDialog("The backend may still send an alert. Closing this screen does not cancel its countdown.", isPresented: $confirmClose, titleVisibility: .visible) {
            Button("Close with result unknown", role: .destructive) { service.closeUnconfirmed() }
        }
    }
}
