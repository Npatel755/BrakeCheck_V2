# N18 — Picture in Picture removed

Removed system PiP controls, controller, live sample delivery, camera multitasking configuration, and the PiP-only audio background mode. Removed the unused floating-camera component. Navigation contains no floating camera tile. The normal AVCaptureVideoPreviewLayer camera preview and foreground tracking guards are restored.

Home, Camera, and Navigate remain separate pages. Switching pages within BrakeCheck retains the same trip and camera session. Leaving the app pauses camera tracking through the existing background finalization path.

Verification: release iOS build; IPA CRC, build/revision metadata, executable identity, all eleven model-resource files, and absence of PiP background modes and feature text. Runtime phone checks remain appropriate after installation: Camera preview, navigation page switching, and camera pause/resume after visiting Home on iOS.
