import BrakeCheckCore
import AVFoundation
import SwiftUI
import UIKit

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    let orientation: AVCaptureVideoOrientation

    func makeUIView(context: Context) -> PreviewSurface {
        let view = PreviewSurface()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspect
        view.appliedOrientation = orientation
        return view
    }

    func updateUIView(_ uiView: PreviewSurface, context: Context) {
        if uiView.previewLayer.session !== session { uiView.previewLayer.session = session }
        uiView.appliedOrientation = orientation
        uiView.applyRotation()
    }
    static func dismantleUIView(_ uiView: PreviewSurface, coordinator: ()) {
        // Release this view's connection, without stopping capture/recording.
        uiView.previewLayer.session = nil
    }
}

final class PreviewSurface: UIView {
    var appliedOrientation: AVCaptureVideoOrientation = .portrait
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    override func layoutSubviews() {
        super.layoutSubviews()
        applyRotation()
    }
    func applyRotation() {
        if let connection = previewLayer.connection, connection.isVideoOrientationSupported,
           connection.videoOrientation != appliedOrientation {
            connection.videoOrientation = appliedOrientation
        }
    }
}

/// Observes the actual app window, so rotation lock and landscape-left/right are respected.
struct InterfaceOrientationObserver: UIViewRepresentable {
    var onChange: (UIInterfaceOrientation) -> Void
    func makeUIView(context: Context) -> OrientationSurface {
        let view = OrientationSurface()
        view.isUserInteractionEnabled = false
        view.onChange = onChange
        return view
    }
    func updateUIView(_ uiView: OrientationSurface, context: Context) {
        uiView.onChange = onChange
        uiView.reportOrientation()
    }
}

final class OrientationSurface: UIView {
    var onChange: ((UIInterfaceOrientation) -> Void)?
    private var lastOrientation: UIInterfaceOrientation = .unknown
    override func didMoveToWindow() { super.didMoveToWindow(); reportOrientation() }
    override func layoutSubviews() { super.layoutSubviews(); reportOrientation() }
    func reportOrientation() {
        guard let orientation = window?.windowScene?.interfaceOrientation,
              CaptureFrameGeometry.videoOrientation(for: orientation) != nil,
              orientation != lastOrientation else { return }
        lastOrientation = orientation
        // Avoid publishing SwiftUI state inside its update/layout transaction.
        DispatchQueue.main.async { [weak self] in self?.onChange?(orientation) }
    }
}

struct TargetOverlay: View {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    let trackedBox: CGRect?
    let selecting: Bool
    @Binding var selectedBox: CGRect?
    var alert = false
    var coasting = false

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color.clear
                // A held visual track is not a fresh detection, so hide its
                // old rectangle while keeping manual selection visible.
                if let box = selecting ? selectedBox : trackedBox, selecting || !coasting {
                    Rectangle()
                        .stroke(selecting ? BrakeTheme.caution : alert ? BrakeTheme.danger : BrakeTheme.accentColor(named: accentChoice),
                                lineWidth: alert ? 3 : 2.5)
                        .frame(width: box.width * geometry.size.width, height: box.height * geometry.size.height)
                        .position(x: box.midX * geometry.size.width, y: (1 - box.midY) * geometry.size.height)
                }
                if selecting, selectedBox == nil {
                    Text("Draw a box around the rear of the parked vehicle")
                        .font(.caption.weight(.semibold)).padding(10)
                        .foregroundStyle(BrakeTheme.text)
                        .background(BrakeTheme.cameraSurface, in: RoundedRectangle(cornerRadius: 8))
                }
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 3).onChanged { drag in
                guard selecting, geometry.size.width > 0, geometry.size.height > 0 else { return }
                let rect = CGRect(x: min(drag.startLocation.x, drag.location.x),
                                  y: min(drag.startLocation.y, drag.location.y),
                                  width: abs(drag.location.x - drag.startLocation.x),
                                  height: abs(drag.location.y - drag.startLocation.y))
                    .intersection(CGRect(origin: .zero, size: geometry.size))
                guard !rect.isNull else { return }
                selectedBox = CGRect(x: rect.minX / geometry.size.width,
                                     y: 1 - rect.maxY / geometry.size.height,
                                     width: rect.width / geometry.size.width,
                                     height: rect.height / geometry.size.height)
            })
            .allowsHitTesting(selecting)
        }
    }
}
