# N17 system Picture in Picture

This build removes the Navigation camera tile and implements AVKit system PiP using a live AVSampleBufferDisplayLayer source. No VoIP mode or restricted entitlement is declared. The audio background mode serves actual PiP video playback.

After configuring the rear camera, the capture queue reads AVCaptureSession.isMultitaskingCameraAccessSupported. It enables isMultitaskingCameraAccessEnabled only when supported. The Camera page's PiP button reports unsupported configurations instead of starting an unusable background camera window. General PiP availability and isPictureInPicturePossible are checked separately.

On a supported configuration, active PiP retains camera capture and the existing tracking pipeline after backgrounding. Navigation speech, impact monitoring and safety audio remain subject to their foreground lifecycle. GPS measurements retain existing freshness rules; PiP does not grant background GPS access. The system window contains camera video; SwiftUI bounding boxes and the danger meter are not baked into its video frames.

The live sample stream is capped at ten frames per second. The watchdog closes PiP if frames cease for 1.5 seconds. Closing PiP while backgrounded resumes the normal camera pause/finalization path. Ending a trip stops PiP. Priority safety audio preempts PiP without releasing the new audio owner's session. Returning through the system PiP restore control opens the Camera page.

## Device verification required

The Windows/WSL build cannot measure the connected iPhone's runtime capability or establish App Store approval. Install N17, start a trip, and tap PiP on Camera. Record the exact status. If supported, start PiP, open Maps, and confirm moving live frames continue beyond thirty seconds. Close PiP, return to Camera, rotate, and verify capture resumes without stale boxes. Test interruption, screen locking, and ending a trip. A support result alone does not prove continuous capture or publication eligibility.

Apple primary references:
- https://developer.apple.com/documentation/avfoundation/avcapturesession/ismultitaskingcameraaccesssupported
- https://developer.apple.com/documentation/avkit/avpictureinpicturecontroller/contentsource-swift.class/init(samplebufferdisplaylayer:playbackdelegate:)
- https://developer.apple.com/videos/play/wwdc2021/10290/
