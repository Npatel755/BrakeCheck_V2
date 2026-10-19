import SwiftUI

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

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("Your people, one check-in away.", systemImage: "person.2.fill").font(.headline)
                    Text("Add a contact, share their invite, and have them text YES to their Photon line. Only confirmed contacts receive alerts. STOP opts them out.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Add a contact") {
                    TextField("First name", text: $name).textContentType(.givenName)
                    TextField("Phone with country code, +1…", text: $phone).keyboardType(.phonePad)
                    Button(busy ? "Adding…" : "Add contact") {
                        busy = true
                        Task {
                            let cleaned = phone.filter { $0.isNumber || $0 == "+" }
                            if await service.addContact(name: name.trimmingCharacters(in: .whitespaces), phone: cleaned) != nil {
                                name = ""; phone = ""
                            }
                            busy = false
                        }
                    }.disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty || phone.isEmpty || service.settings.client == nil)
                    if let error = service.error { Text(error).foregroundStyle(.orange).font(.caption) }
                }
                Section("Emergency contacts") {
                    Text("Send a clearly marked test to a confirmed contact. Test messages never include your location.")
                        .font(.caption).foregroundStyle(.secondary)
                    if service.contacts.isEmpty { Text("No contacts yet").foregroundStyle(.secondary) }
                    ForEach(service.contacts) { contact in
                        VStack(alignment: .leading, spacing: 10) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(contact.first_name).font(.headline)
                                Text(contact.phone).font(.caption).foregroundStyle(.secondary)
                                Label(contact.confirmed ? "Ready for alerts" : "Waiting for YES", systemImage: contact.confirmed ? "checkmark.seal.fill" : "clock")
                                    .font(.caption).foregroundStyle(contact.confirmed ? BrakeTheme.accent : Color.orange)
                            }
                            HStack(spacing: 18) {
                                ShareLink(item: contact.invite_text) { Label("Invite", systemImage: "square.and.arrow.up") }
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
                        }
                    }
                    Button("Refresh confirmations") { Task { await service.refreshContacts() } }
                }
            }
            .navigationTitle("Emergency contacts").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task { await service.refreshContacts() }
            .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await service.refreshContacts() } } }
            .refreshable { await service.refreshContacts() }
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
