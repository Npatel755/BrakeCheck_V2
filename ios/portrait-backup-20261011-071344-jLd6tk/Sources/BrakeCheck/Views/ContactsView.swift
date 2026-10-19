import SwiftUI
import MessageUI

struct ContactsView: View {
    @ObservedObject var service: SafetyService
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var name = ""
    @State private var phone = ""
    @State private var busy = false
    @State private var deleting: EmergencyContact?
    @State private var testTarget: EmergencyContact?
    @State private var sendingTestFor: String?
    @State private var testOutcome: ContactTestOutcome?
    @State private var inviting: EmergencyContact?
    @State private var invitationNotice: String?
    private var pendingContactIDs: [String] {
        service.contacts.filter { !$0.confirmed }.map(\.id).sorted()
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("Your people, one check-in away.", systemImage: "person.2.fill").font(.headline)
                    Text("Adding a contact opens their Messages invitation. Tap Send. They open the link and send YES to the BrakeCheck number to confirm. Only confirmed contacts receive alerts; STOP opts them out.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Add a contact") {
                    TextField("First name", text: $name).textContentType(.givenName)
                    TextField("Phone with country code, +1…", text: $phone).keyboardType(.phonePad)
                    Button(busy ? "Adding…" : "Add contact") {
                        busy = true
                        Task {
                            let cleaned = phone.filter { $0.isNumber || $0 == "+" }
                            if let contact = await service.addContact(name: name.trimmingCharacters(in: .whitespaces), phone: cleaned) {
                                name = ""; phone = ""
                                invitationNotice = nil
                                if !contact.confirmed { inviting = contact }
                                else { invitationNotice = "\(contact.first_name) is already confirmed and ready for alerts." }
                            }
                            busy = false
                        }
                    }.disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty || phone.isEmpty || service.settings.client == nil)
                    if let error = service.error { Text(error).foregroundStyle(.orange).font(.caption) }
                }
                Section("Emergency contacts") {
                    if let invitationNotice {
                        Text(invitationNotice).font(.caption).foregroundStyle(.secondary)
                    }
                    Text("Send a clearly marked test to a confirmed contact. Test messages never include your location.")
                        .font(.caption).foregroundStyle(.secondary)
                    if service.contacts.isEmpty { Text("No contacts yet").foregroundStyle(.secondary) }
                    ForEach(service.contacts) { contact in
                        VStack(alignment: .leading, spacing: 10) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(contact.first_name).font(.headline)
                                Text(contact.phone).font(.caption).foregroundStyle(.secondary)
                                Label(contact.removalPending ? "Removal pending · retry Remove"
                                      : contact.confirmed ? "Ready for alerts" : "Waiting for YES",
                                      systemImage: contact.confirmed ? "checkmark.seal.fill" : "clock")
                                    .font(.caption).foregroundStyle(contact.confirmed ? BrakeTheme.accent : Color.orange)
                            }
                            HStack(spacing: 18) {
                                Button { inviting = contact; invitationNotice = nil } label: {
                                    Label("Invite", systemImage: "message")
                                }
                                .accessibilityHint("Opens a message addressed to this contact with their confirmation invitation.")
                                .disabled(contact.removalPending)
                                Button {
                                    testTarget = contact
                                } label: {
                                    if sendingTestFor == contact.id {
                                        Label("Sending…", systemImage: "hourglass")
                                    } else {
                                        Label("Test message", systemImage: "paperplane")
                                    }
                                }
                                .disabled(!contact.confirmed || sendingTestFor != nil || service.settings.client == nil)
                                .accessibilityHint("Sends this confirmed contact a test message without your location.")
                                Spacer(minLength: 0)
                                Button { deleting = contact } label: { Image(systemName: "trash") }
                                    .tint(.red).accessibilityLabel("Remove \(contact.first_name)")
                            }
                            .buttonStyle(.borderless)
                        }
                    }
                    Button("Refresh confirmations") { Task { await service.refreshContacts() } }
                }
            }
            .navigationTitle("Emergency contacts").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task { await service.refreshContacts() }
            .task(id: pendingContactIDs) {
                guard !pendingContactIDs.isEmpty, service.settings.client != nil else { return }
                while !Task.isCancelled {
                    do { try await Task.sleep(nanoseconds: 5_000_000_000) }
                    catch { return }
                    guard !Task.isCancelled else { return }
                    if scenePhase == .active { await service.refreshContacts() }
                }
            }
            .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await service.refreshContacts() } } }
            .refreshable { await service.refreshContacts() }
            .sheet(item: $inviting, onDismiss: { Task { await service.refreshContacts() } }) { contact in
                if MFMessageComposeViewController.canSendText() {
                    ContactInviteComposer(contact: contact) { result in
                        inviting = nil
                        switch result {
                        case .sent:
                            invitationNotice = "Invitation submitted to Messages. Waiting for \(contact.first_name) to send YES."
                        case .cancelled:
                            invitationNotice = "Contact saved. Tap Invite to send the confirmation invitation later."
                        case .failed:
                            service.error = "Messages could not send the invitation. Tap Invite to try again."
                        @unknown default:
                            service.error = "Messages did not confirm the invitation result."
                        }
                    }
                    .ignoresSafeArea()
                } else {
                    ContactInviteFallback(contact: contact)
                }
            }
            .confirmationDialog("Remove this emergency contact?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                Button("Remove contact", role: .destructive) {
                    if let contact = deleting { Task { await service.removeContact(contact) } }
                    deleting = nil
                }
            }
            .confirmationDialog("Send a test message?", isPresented: Binding(get: { testTarget != nil }, set: { if !$0 { testTarget = nil } }), titleVisibility: .visible) {
                Button("Send test message") {
                    guard let contact = testTarget else { return }
                    testTarget = nil
                    sendingTestFor = contact.id
                    Task {
                        testOutcome = await service.sendTestMessage(to: contact)
                        sendingTestFor = nil
                    }
                }
                Button("Cancel", role: .cancel) { testTarget = nil }
            } message: {
                Text("\(testTarget?.first_name ?? "This contact") will receive a message labeled BRAKECHECK TEST ONLY. It says there is no emergency and no location is shared. Ask them to confirm it arrived.")
            }
            .alert("Emergency contact test", isPresented: Binding(get: { testOutcome != nil }, set: { if !$0 { testOutcome = nil } })) {
                Button("OK", role: .cancel) { testOutcome = nil }
            } message: {
                Text(testOutcome?.detail ?? "")
            }
        }.tint(BrakeTheme.accent)
    }
}

private func contactInvitationText(_ contact: EmergencyContact) -> String {
    contact.invite_text + "\n\nOpen the link, then send YES to the BrakeCheck number to confirm. Reply STOP to that number to opt out."
}

/// The system composer opens with the server-created invitation and recipient.
/// Only the user can tap Send; submission never marks a contact as confirmed.
private struct ContactInviteComposer: UIViewControllerRepresentable {
    let contact: EmergencyContact
    let onFinish: (MessageComposeResult) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }
    func makeUIViewController(context: Context) -> MFMessageComposeViewController {
        let controller = MFMessageComposeViewController()
        controller.recipients = [contact.phone]
        controller.body = contactInvitationText(contact)
        controller.messageComposeDelegate = context.coordinator
        return controller
    }
    func updateUIViewController(_ controller: MFMessageComposeViewController, context: Context) {}

    final class Coordinator: NSObject, MFMessageComposeViewControllerDelegate {
        let onFinish: (MessageComposeResult) -> Void
        init(onFinish: @escaping (MessageComposeResult) -> Void) { self.onFinish = onFinish }
        func messageComposeViewController(_ controller: MFMessageComposeViewController,
                                          didFinishWith result: MessageComposeResult) {
            onFinish(result)
        }
    }
}

/// A device without Messages still presents the invitation immediately.
private struct ContactInviteFallback: View {
    let contact: EmergencyContact
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                Section("Invite \(contact.first_name)") {
                    Text(contact.phone).font(.caption).foregroundStyle(.secondary)
                    Text("Messages is unavailable on this device. Share this invitation with your contact so they can open the link and send YES to confirm.")
                    ShareLink(item: contactInvitationText(contact)) {
                        Label("Share confirmation invitation", systemImage: "square.and.arrow.up")
                    }
                }
                Section("Invitation") {
                    Text(contactInvitationText(contact)).textSelection(.enabled)
                }
            }
            .navigationTitle("Send invitation").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }.tint(BrakeTheme.accent)
    }
}
