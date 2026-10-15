/// What this build includes. Debug builds (`xtool dev`, Xcode Run) keep the hackathon
/// services so they can still be developed and tested. Release builds (TestFlight and
/// the App Store) are the phone-only v1: everything runs on the iPhone, with no backend.
enum AppScope {
    #if DEBUG
    /// QR pairing, the backend connection, emergency contacts through Photon,
    /// ElevenLabs voice and the server-run crash check-in.
    static let connectedServices = true
    /// Measurement diagnostics and the revision tag in the status pill.
    static let developerTools = true
    /// The trained lane and road models can't ship in a paid app: SegFormer's NVIDIA licence is
    /// non-commercial, and UFLD's weights were trained on CULane (non-commercial). Without them
    /// LaneTracker uses its own lane-paint scanner, and the camera's AR road ribbon stays off
    /// (directions still show in the banner). The IPA build also strips their files.
    static let neuralLanes = true
    #else
    static let connectedServices = false
    static let developerTools = false
    static let neuralLanes = false
    #endif
}
