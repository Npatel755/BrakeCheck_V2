# Mock video + GPS AR test

Tracking follow-up: `Open-AR-Tracking-Test.cmd` or `Open-AR-Continuity-Test.cmd`
opens the current successive-frame tracking test. See
`reports/ar-mock-test-20261023/SUSTAINED.md` for scope, unresolved cases and tests.
This experimental path is not enabled in the live iPhone app.

Open `Open-AR-Mock-Test.cmd` at the workspace root, or open
`reports/ar-mock-test-20261023/preview.html` directly. No server, GPS hardware,
network or iPhone is needed to watch the generated preview. Keep the HTML beside
its MP4 files. Chapter buttons seek the video; the GPS map and status use that
same playback clock. Video controls provide pause, scrubbing and playback speed.

The 64-second mock drive includes straight road, left/right bends, a 90-degree
right turn, and lane/GPS/obstacle/road/mount/camera/overload failures. A fixture
generator creates the road geometry, video and GPS on one deterministic clock.
GPS is sampled at 5 Hz, mock AR evidence at 10 Hz, video at 30 fps. The JSON
contains the complete input data, with capture times, course, speed and accuracy;
the GPX is an export of the GPS samples, including intentional stale fixes.

The Swift runner imports the actual `BrakeCheckCore` from the current app
worktree. `MockARReplay` calls production `NavigationProgressTracker`,
`ARInferencePolicy`, `CameraARConfidencePolicy`, `CameraARConfidenceTracker`,
`CameraRouteProjection`, `CameraRouteSmoother`, and road visibility masking.
The preview draws that returned geometry; it never forces alignment or adds a
curved route where the core returns none. The current intersection behavior is
a direction arrow, followed by fresh-frame confirmation after the turn.

Video, GPS **and perception** are synthetic. This does not execute UFLD, vehicle
detection, Core ML or real GPS; workload events are supplied numbers rather than
a PC/phone benchmark. It verifies overlay and fallback behavior with controlled
inputs, not real-road accuracy or a fix for V9's performance pauses. Live driving
services, model weights and the existing N69 IPA are unchanged.

## Rebuild

From the workspace root:

```powershell
& tools/ar_mock_test/Run-Test.ps1 -Open
```

Dependencies already used on this PC: Python with OpenCV/NumPy, the local
`imageio-ffmpeg` installation under `test-results/ar-mock-20261023/python-deps`,
Ubuntu-24.04 WSL, and Swift at `/home/nick/.local/share/swiftly/bin/swift`.
If the FFmpeg helper is missing:

```powershell
python -m pip install --target test-results/ar-mock-20261023/python-deps imageio-ffmpeg
```

`verify.py` checks all fallback windows, supported-road recovery, obstacle
exclusion, timestamps, export hashes and full decoding of both videos.
`check_viewer.py` additionally checks local-file playback, seeking, pause and
GPS-panel synchronization in Edge at desktop/narrow widths. It uses the existing
dataset-pipeline environment:

```powershell
tools/dataset_pipeline/.venv/Scripts/python.exe tools/ar_mock_test/check_viewer.py
```

`MockARReplayTests.swift` adds focused Core checks for confirmation, missing
evidence, stale GPS/camera, overload, obstacle holes and malformed timelines.
Run the normal Core release test suite for those tests.
