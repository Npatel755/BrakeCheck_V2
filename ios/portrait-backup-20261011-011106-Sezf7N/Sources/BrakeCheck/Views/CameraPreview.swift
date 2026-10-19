import BrakeCheckCore
import AVFoundation
import SwiftUI

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewSurface {
        let view = PreviewSurface()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspect
        return view
    }

    func updateUIView(_ uiView: PreviewSurface, context: Context) {
        if let connection = uiView.previewLayer.connection, connection.isVideoOrientationSupported {
            connection.videoOrientation = .landscapeRight
        }
    }
}

final class PreviewSurface: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    override func layoutSubviews() {
        super.layoutSubviews()
        if let connection = previewLayer.connection, connection.isVideoOrientationSupported {
            connection.videoOrientation = .landscapeRight
        }
    }
}

struct TargetOverlay: View {
    let trackedBox: CGRect?
    let selecting: Bool
    @Binding var selectedBox: CGRect?
    var alert = false
    var coasting = false

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color.clear
                if let box = selecting ? selectedBox : trackedBox {
                    Rectangle()
                        .stroke(selecting || coasting ? Color.yellow : alert ? BrakeTheme.danger : Color.cyan,
                                style: StrokeStyle(lineWidth: alert ? 3 : 2.5, dash: selecting || coasting ? [6] : []))
                        .frame(width: box.width * geometry.size.width, height: box.height * geometry.size.height)
                        .position(x: box.midX * geometry.size.width, y: (1 - box.midY) * geometry.size.height)
                }
                if selecting, selectedBox == nil {
                    Text("Draw a box around the rear of the parked vehicle")
                        .font(.caption.weight(.semibold)).padding(10)
                        .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 8))
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
