import AVFoundation
import SwiftUI
import UIKit

struct QRCodeScanner: UIViewControllerRepresentable {
    let onCode: (String) -> Void
    let onError: (String) -> Void

    func makeUIViewController(context: Context) -> QRScannerController {
        QRScannerController(onCode: onCode, onError: onError)
    }

    func updateUIViewController(_ controller: QRScannerController, context: Context) {}
}

final class QRScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    private let session = AVCaptureSession()
    private let metadataOutput = AVCaptureMetadataOutput()
    private let sessionQueue = DispatchQueue(label: "com.brakecheck.qr-scanner")
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private var configured = false
    private var sessionReady = false
    private var isVisible = false
    private var scanned = false
    private let onCode: (String) -> Void
    private let onError: (String) -> Void

    init(onCode: @escaping (String) -> Void, onError: @escaping (String) -> Void) {
        self.onCode = onCode
        self.onError = onError
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configureSession()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                DispatchQueue.main.async {
                    if granted {
                        if self?.isVisible == true { self?.configureSession() }
                    }
                    else { self?.showError("Allow camera access in iPhone Settings to scan the pairing QR code.") }
                }
            }
        default:
            showError("Allow camera access in iPhone Settings to scan the pairing QR code.")
        }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        isVisible = true
        if AVCaptureDevice.authorizationStatus(for: .video) == .authorized {
            configureSession()
        }
        startIfVisible()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        isVisible = false
        sessionQueue.async { [session = self.session] in
            if session.isRunning { session.stopRunning() }
        }
    }

    private func startIfVisible() {
        guard isVisible, sessionReady, !scanned else { return }
        sessionQueue.async { [session] in
            if !session.isRunning { session.startRunning() }
        }
    }

    private func configureSession() {
        guard !configured else { return }
        configured = true
        sessionQueue.async { [weak self] in
            guard let self else { return }
            do {
                guard let camera = AVCaptureDevice.default(for: .video) else {
                    throw ScannerError.cameraUnavailable
                }
                let input = try AVCaptureDeviceInput(device: camera)
                guard session.canAddInput(input), session.canAddOutput(metadataOutput) else {
                    throw ScannerError.cameraUnavailable
                }
                session.beginConfiguration()
                session.addInput(input)
                session.addOutput(metadataOutput)
                metadataOutput.setMetadataObjectsDelegate(self, queue: .main)
                guard metadataOutput.availableMetadataObjectTypes.contains(.qr) else {
                    session.commitConfiguration()
                    throw ScannerError.qrUnavailable
                }
                metadataOutput.metadataObjectTypes = [.qr]
                session.commitConfiguration()

                DispatchQueue.main.async {
                    let layer = AVCaptureVideoPreviewLayer(session: self.session)
                    layer.videoGravity = .resizeAspectFill
                    layer.frame = self.view.bounds
                    self.view.layer.insertSublayer(layer, at: 0)
                    self.previewLayer = layer
                    self.sessionReady = true
                    self.startIfVisible()
                }
            } catch {
                DispatchQueue.main.async { self.showError(error.localizedDescription) }
            }
        }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput,
                        didOutput metadataObjects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !scanned,
              let value = metadataObjects.compactMap({ $0 as? AVMetadataMachineReadableCodeObject })
                .first(where: { $0.type == .qr })?.stringValue else { return }
        scanned = true
        sessionQueue.async { [session = self.session] in
            if session.isRunning { session.stopRunning() }
        }
        onCode(value)
    }

    private func showError(_ message: String) {
        let label = UILabel()
        label.text = message
        label.textColor = .white
        label.textAlignment = .center
        label.numberOfLines = 0
        label.font = .systemFont(ofSize: 16, weight: .medium)
        label.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            label.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),
            label.centerYAnchor.constraint(equalTo: view.centerYAnchor)
        ])
        onError(message)
    }

    private enum ScannerError: LocalizedError {
        case cameraUnavailable
        case qrUnavailable

        var errorDescription: String? {
            switch self {
            case .cameraUnavailable: return "The iPhone camera is not available for pairing."
            case .qrUnavailable: return "This iPhone could not start QR scanning."
            }
        }
    }
}

struct PairingScannerSheet: View {
    // This closure receives the ENTIRE QR JSON, not just the one-time code.
    let onCode: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var scanError: String?

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottom) {
                QRCodeScanner(onCode: handleScan, onError: { scanError = $0 })
                    .ignoresSafeArea(edges: .bottom)
                Text("Scan the one-time QR code from your BrakeSense backend.")
                    .font(.brake(.callout, weight: .medium))
                    .multilineTextAlignment(.center)
                    .padding(14)
                    .frame(maxWidth: .infinity)
                    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
                    .padding()
            }
            .navigationTitle("Pair with backend")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .alert("Can't use this QR code", isPresented: Binding(
                get: { scanError != nil },
                set: { if !$0 { scanError = nil } }
            )) {
                Button("OK", role: .cancel) { scanError = nil }
            } message: {
                Text(scanError ?? "")
            }
        }
        .tint(BrakeTheme.accent)
    }

    private func handleScan(_ rawValue: String) {
        guard let data = rawValue.data(using: .utf8),
              let payload = try? JSONDecoder().decode(PairingScannerPayload.self, from: data),
              payload.app == "BrakeCheck", payload.version == 1,
              (32...128).contains(payload.code.count),
              let url = URL(string: payload.backend_url),
              let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let host = url.host, !host.isEmpty,
              url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else {
            scanError = "Invalid BrakeSense pairing QR code. Generate a fresh QR code with `python pair.py`; for a hosted backend, include its HTTPS URL with `--backend-url`."
            return
        }

        // Pass the full JSON to ConnectionSettings.pair(qrString:deviceName:).
        // ConnectionSettings independently validates the destination before sending a code.
        onCode(rawValue)
        dismiss()
    }
}

// A scanner-specific name avoids conflicting with any other pairing model.
private struct PairingScannerPayload: Decodable {
    let app: String
    let version: Int
    let code: String
    let backend_url: String
}
