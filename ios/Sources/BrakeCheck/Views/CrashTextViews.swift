import Contacts
import ContactsUI
import MessageUI
import SwiftUI
import UIKit

// MARK: - Check-in screen

/// Full-screen "Are you okay?" for the phone-only crash check-in, plus the one-tap composer.
struct CrashTextCover: ViewModifier {
    @ObservedObject var flow: CrashTextFlow

    func body(content: Content) -> some View {
        content
            .fullScreenCover(isPresented: $flow.presented) { CrashTextCheckinView(flow: flow) }
            .onOpenURL { _ = flow.handle($0) }
    }
}

struct CrashTextCheckinView: View {
    @ObservedObject var flow: CrashTextFlow

    private var ringColor: Color {
        flow.remaining <= 5 ? BrakeTheme.danger : flow.remaining <= 10 ? BrakeTheme.warning : BrakeTheme.accent
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 28) {
                switch flow.phase {
                case .asking: asking
                case .waitingForShortcut:
                    status(title: "Sending your crash text…", detail: "Your BrakeCheck SOS Shortcut is sending it. If it doesn't, BrakeCheck opens a ready-to-send text instead.")
                    ProgressView()
                case .composing, .idle:
                    status(title: "Tap Send to text your contacts", detail: "iPhone needs one tap to send a text. Your location is already in the message.")
                    Button("Open the text again") { flow.retryComposer() }.buttonStyle(.borderedProminent).tint(BrakeTheme.danger)
                case .finished(let title, let detail):
                    status(title: title, detail: detail)
                    if title == "Text not sent" {
                        Button("Open the text again") { flow.retryComposer() }.buttonStyle(.borderedProminent).tint(BrakeTheme.danger)
                    }
                    Button("Done") { flow.close() }.buttonStyle(.bordered).tint(BrakeTheme.accent)
                }
            }
            .padding(28)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .background(BrakeTheme.background.ignoresSafeArea())
        .interactiveDismissDisabled()
        .sheet(isPresented: $flow.composerShown) {
            if MFMessageComposeViewController.canSendText() {
                CrashTextComposer(recipients: flow.recipients, body: flow.draft) { flow.composerFinished(sent: $0) }
                    .ignoresSafeArea()
            } else {
                CrashTextNoMessages(recipients: flow.recipients, text: flow.draft) { flow.composerFinished(sent: false) }
            }
        }
    }

    private var asking: some View {
        VStack(spacing: 26) {
            ZStack {
                Circle().stroke(ringColor.opacity(0.14), lineWidth: 10)
                Circle().trim(from: 0, to: CGFloat(flow.remaining) / CGFloat(CrashTextFlow.countdownSeconds))
                    .stroke(ringColor, style: StrokeStyle(lineWidth: 10, lineCap: .round)).rotationEffect(.degrees(-90))
                    .animation(.linear(duration: 0.3), value: flow.remaining)
                Text("\(flow.remaining)").font(.system(size: 60, weight: .light, design: .rounded)).monospacedDigit()
            }
            .frame(width: 190, height: 190)
            Text(flow.practice ? "PRACTICE · NOBODY IS TEXTED" : "POSSIBLE CRASH")
                .font(.system(size: 12, weight: .bold)).tracking(1.2).foregroundStyle(ringColor)
            VStack(spacing: 8) {
                Text("Are you okay?").font(.system(size: 34, weight: .semibold, design: .rounded))
                Text("If you don't answer, BrakeCheck texts your emergency contacts your location.")
                    .font(.subheadline).foregroundStyle(BrakeTheme.muted).multilineTextAlignment(.center)
            }
            VStack(spacing: 12) {
                Button { flow.imOkay() } label: {
                    Label("I'm okay", systemImage: "checkmark").font(.title3.weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 64)
                }
                .buttonStyle(.borderedProminent).tint(BrakeTheme.accent).foregroundStyle(BrakeTheme.background)
                Button { flow.sendHelp() } label: {
                    Label("Text my contacts now", systemImage: "exclamationmark.bubble.fill").font(.title3.weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 64)
                }
                .buttonStyle(.borderedProminent).tint(BrakeTheme.danger)
            }
        }
    }

    private func status(title: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Text(title).font(.system(size: 28, weight: .semibold, design: .rounded)).multilineTextAlignment(.center)
            Text(detail).font(.subheadline).foregroundStyle(BrakeTheme.muted).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 60)
    }
}

/// The system composer with the crash text filled in. Only the user can tap Send.
private struct CrashTextComposer: UIViewControllerRepresentable {
    let recipients: [String]
    let body: String
    let onFinish: (Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }
    func makeUIViewController(context: Context) -> MFMessageComposeViewController {
        let controller = MFMessageComposeViewController()
        controller.recipients = recipients
        controller.body = body
        controller.messageComposeDelegate = context.coordinator
        return controller
    }
    func updateUIViewController(_ controller: MFMessageComposeViewController, context: Context) {}

    final class Coordinator: NSObject, MFMessageComposeViewControllerDelegate {
        let onFinish: (Bool) -> Void
        init(onFinish: @escaping (Bool) -> Void) { self.onFinish = onFinish }
        func messageComposeViewController(_ controller: MFMessageComposeViewController,
                                          didFinishWith result: MessageComposeResult) {
            onFinish(result == .sent)
        }
    }
}

/// Devices without Messages (an iPod, or Messages removed) can still share the text.
private struct CrashTextNoMessages: View {
    let recipients: [String]
    let text: String
    let onClose: () -> Void
    var body: some View {
        NavigationStack {
            Form {
                Section("Messages isn't available") {
                    Text("Share this text another way, or call: \(recipients.joined(separator: ", "))")
                    ShareLink(item: text) { Label("Share the crash text", systemImage: "square.and.arrow.up") }
                }
                Section("Text") { Text(text).textSelection(.enabled) }
            }
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Close", action: onClose) } }
        }
    }
}

// MARK: - Setup

/// Contacts, crash sensing and the optional Shortcut. Reached from Home (the prompt after the
/// first trip, or the Crash texts row); never part of first-launch onboarding.
struct CrashTextSetupView: View {
    @ObservedObject var model: DriveModel
    @ObservedObject var flow: CrashTextFlow
    @AppStorage("crashText.senderName") private var senderName = ""
    @State private var contacts = CrashTextStore.shared.contacts
    @State private var newName = ""
    @State private var newPhone = ""
    @State private var addingManually = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        Form {
            Section {
                Label("Text someone if you crash", systemImage: "message.badge.waveform.fill").font(.headline)
                Text("If BrakeCheck senses a possible crash during a trip, it asks if you're okay. If you ask for help or don't answer in \(CrashTextFlow.countdownSeconds) seconds, it texts these people where you are.")
                    .font(.caption).foregroundStyle(BrakeTheme.muted)
            }

            Section("Who to text") {
                ForEach(contacts) { contact in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(contact.name).font(.headline)
                        Text(contact.phone).font(.caption).foregroundStyle(BrakeTheme.muted)
                    }
                }
                .onDelete { contacts.remove(atOffsets: $0); save() }
                Button { ContactPicker.present { add(name: $0, phone: $1) } } label: {
                    Label("Add from Contacts", systemImage: "person.crop.circle.badge.plus")
                }
                if addingManually {
                    TextField("Name", text: $newName).textContentType(.name)
                    TextField("Phone number", text: $newPhone).keyboardType(.phonePad).textContentType(.telephoneNumber)
                    Button("Add") { add(name: newName, phone: newPhone); newName = ""; newPhone = "" }
                        .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty || cleaned(newPhone).count < 7)
                } else {
                    Button("Type a number instead") { addingManually = true }
                }
            }

            Section {
                TextField("Your first name (optional)", text: $senderName).textContentType(.givenName)
            } footer: {
                Text("So the text says who it's about.")
            }

            Section {
                Toggle("Watch for crashes during trips", isOn: $model.impactEnabled).disabled(model.demo)
            } footer: {
                Text("Experimental. A dropped phone can set it off; just tap I'm okay.")
            }

            Section("Send automatically (optional)") {
                if flow.shortcutReady {
                    Label("Automatic sending is on", systemImage: "checkmark.seal.fill").foregroundStyle(BrakeTheme.accent)
                } else {
                    Text("Without this, BrakeCheck opens a ready-to-send text and you tap Send. With it, the text goes out even if you can't tap.")
                        .font(.caption).foregroundStyle(BrakeTheme.muted)
                }
                if let link = URL(string: CrashTextStore.shortcutLink), !CrashTextStore.shortcutLink.isEmpty {
                    Button { openURL(link) } label: {
                        Label("1. Add the \(CrashTextStore.shortcutName) Shortcut", systemImage: "plus.square.on.square")
                    }
                    Button { flow.checkShortcut() } label: {
                        Label(flow.checking ? "Checking…" : "2. Check it works", systemImage: "checkmark.circle")
                    }.disabled(flow.checking)
                } else {
                    Button { flow.checkShortcut() } label: {
                        Label(flow.checking ? "Checking…" : "Check the \(CrashTextStore.shortcutName) Shortcut", systemImage: "checkmark.circle")
                    }.disabled(flow.checking)
                    Text("The one-tap Shortcut link isn't published yet. If you built the Shortcut yourself, keep the name \"\(CrashTextStore.shortcutName)\" and tap Check.")
                        .font(.caption).foregroundStyle(BrakeTheme.muted)
                }
                if let status = flow.checkStatus { Text(status).font(.caption) }
            }

            Section {
                Button("Practice the check-in") { flow.begin(location: nil, practice: true) }
            } footer: {
                Text("A practice run never texts anyone.")
            }

            Section {
                Text("BrakeCheck can't call 911. On iPhone 14 and later, Apple's Crash Detection can, and it texts your emergency contacts too. Check it's on in Settings › Emergency SOS.")
                    .font(.caption).foregroundStyle(BrakeTheme.muted)
            }
        }
        .navigationTitle("Crash texts").navigationBarTitleDisplayMode(.inline)
        .tint(BrakeTheme.accent)
    }

    private func cleaned(_ phone: String) -> String { phone.filter { $0.isNumber || $0 == "+" } }

    private func add(name: String, phone: String) {
        let number = cleaned(phone)
        guard number.count >= 7, !contacts.contains(where: { cleaned($0.phone) == number }) else { return }
        contacts.append(CrashTextContact(name: name.trimmingCharacters(in: .whitespacesAndNewlines), phone: number))
        save()
    }

    private func save() {
        CrashTextStore.shared.contacts = contacts
        // Turning crash texts on without crash sensing would do nothing.
        if !contacts.isEmpty && !model.impactEnabled && !model.demo { model.impactEnabled = true }
    }
}

/// Apple's contact picker needs no Contacts permission. It's presented from UIKit because
/// wrapping it in a SwiftUI sheet makes it dismiss itself immediately.
@MainActor
private enum ContactPicker {
    private static var delegate: Delegate?

    static func present(onPick: @escaping (String, String) -> Void) {
        let picker = CNContactPickerViewController()
        picker.displayedPropertyKeys = [CNContactPhoneNumbersKey]
        picker.predicateForEnablingContact = NSPredicate(format: "phoneNumbers.@count > 0")
        picker.predicateForSelectionOfContact = NSPredicate(format: "phoneNumbers.@count == 1")
        let delegate = Delegate(onPick: onPick)
        Self.delegate = delegate
        picker.delegate = delegate
        guard let root = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene })
            .flatMap(\.windows).first(where: \.isKeyWindow)?.rootViewController else { return }
        var top = root
        while let presented = top.presentedViewController { top = presented }
        top.present(picker, animated: true)
    }

    private final class Delegate: NSObject, CNContactPickerDelegate {
        let onPick: (String, String) -> Void
        init(onPick: @escaping (String, String) -> Void) { self.onPick = onPick }

        func contactPicker(_ picker: CNContactPickerViewController, didSelect contact: CNContact) {
            if let number = contact.phoneNumbers.first?.value.stringValue { onPick(name(contact), number) }
        }
        func contactPicker(_ picker: CNContactPickerViewController, didSelect contactProperty: CNContactProperty) {
            if let number = (contactProperty.value as? CNPhoneNumber)?.stringValue { onPick(name(contactProperty.contact), number) }
        }
        private func name(_ contact: CNContact) -> String {
            let full = [contact.givenName, contact.familyName].filter { !$0.isEmpty }.joined(separator: " ")
            return full.isEmpty ? "Emergency contact" : full
        }
    }
}

// MARK: - Home prompt

/// Shown on Home after the first trip, until crash texts are set up or dismissed.
struct CrashTextPromptCard: View {
    var setUp: () -> Void
    var dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "message.badge.waveform.fill")
                    .font(.system(size: 22)).foregroundStyle(BrakeTheme.accent)
                    .frame(width: 44, height: 44)
                    .background(BrakeTheme.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Text someone if you crash?").font(.system(size: 17, weight: .semibold))
                    Text("BrakeCheck can text your people where you are if it senses a crash. Takes about 10 seconds.")
                        .font(.subheadline).foregroundStyle(BrakeTheme.muted).fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack(spacing: 12) {
                Button(action: setUp) {
                    Text("Set up").font(.subheadline.weight(.bold)).frame(maxWidth: .infinity, minHeight: 44)
                        .foregroundStyle(BrakeTheme.background).background(BrakeTheme.accent, in: Capsule())
                }
                Button(action: dismiss) {
                    Text("Not now").font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 44)
                        .foregroundStyle(BrakeTheme.muted)
                }
            }
            .buttonStyle(.plain)
        }
        .brakePanel()
    }
}
