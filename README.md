# BrakeCheck

A native iPhone driving prototype with on-device YOLO vehicle detection, GPS speed, a rolling dashcam, ElevenLabs voice check-ins, and Photon emergency-contact messages. This merges the supplied RoadWatch xtool project with the Brake Check side-pot services.

**Start here:** for Railway hosting, follow [README-RAILWAY.md](README-RAILWAY.md). Build the iPhone app from `ios/`. For a same-Wi-Fi demo, run the Python backend and Photon agent on your computer. The earlier [Koyeb deployment guide](cloud/README.md) remains available. The app does not embed vendor secret keys.

## What changed

- Renamed the Swift package, core module, app entry point, display name, and bundle ID to BrakeCheck.
- Replaced the dashboard with a dark, mint-accented landscape layout: a large road view, prominent GPS speed, risk indication, and compact metrics. Diagnostics, calibration, incidents, settings, and contacts have dedicated controls.
- Added cached ElevenLabs brake alerts, a post-impact spoken check-in, a six-second microphone reply, and Scribe transcription. English and Spanish audio are supported. Urgent warnings use device speech if the downloaded alert is unavailable.
- Added contact registration, shareable opt-in invitations, confirmation status, removal, and inbound YES/STOP handling.
- Added a confirmed-contact-only test message with a confirmation step, a location-free `BRAKECHECK TEST ONLY` message, and a server-side cooldown.
- Added five-minute QR pairing, Bonjour discovery, and per-phone Keychain tokens so the iPhone no longer needs the backend app key or a typed IPv4 address.
- Added Photon crash messages and replies to **where**, **clip**, **status**, and **drive**. Ending a real trip sends a summary through the backend.
- Added a message-free check-in demo, manual “I'm okay” and “Send help” buttons, backend-owned deadlines, and duplicate-send protection.
- Fixed false delivery success: `alerted` is true only when Photon reports at least one accepted send. Message acceptance is not a read receipt.
- Restored the supplied YOLO model into the resource folder used by the xtool package. Detection and video recording remain on the phone.

## 1. Configure the backend on Windows

Use Python 3.11 or newer. From the extracted `BrakeCheck` folder in PowerShell:

```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe configure.py
```

Enter the ElevenLabs API key, Photon project ID, and Photon project secret you received. The two secret prompts do not echo your input. The script creates `backend/.env`, sets `DRIVER_NAME=Harrison`, and generates separate app and agent connection keys. It refuses to overwrite an existing `.env`.

Keep **BRAKECHECK_APP_KEY** on the computer; it authorizes the local pairing utility and is no longer entered on the iPhone. Keep **AGENT_SHARED_KEY** between the backend and agent only. Vendor keys never go into Swift files, Info.plist, screenshots, or a public repository. The supplied `.gitignore` excludes the credential file, contacts, paired-device records, voice cache, logs, build outputs, and dependencies.

Start the backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Use **one worker** and avoid `--reload` during a check-in: the demo's crash state and timers live in one Python process. Leave this terminal open.

On macOS/Linux, replace the Windows venv commands with `python3 -m venv .venv` and `.venv/bin/python`.

## 2. Start the Photon agent

**Coordinate with Harrison first so he can stop his existing agent.** This project does not start an agent automatically or send him a message. Two agents connected to the same project can produce duplicate replies.

In a second terminal, with Node.js 20.6+ installed:

```powershell
cd BrakeCheck\photon-agent
npm ci
npm start
```

Adjust `cd` to the extracted folder's actual location. `npm start` reads `../backend/.env`; the agent listens on `127.0.0.1:8787`. Run it on the same computer/environment as the Python backend. Vendor project credentials are required even though the local HTTP agent endpoint is loopback-only.

## 3. Build and install the iPhone app

Keep using your existing xtool/WSL setup. Open **BrakeCheck/ios**, the folder containing `Package.swift` and `xtool.yml`, in VS Code's WSL environment:

```bash
cd /path/to/BrakeCheck/ios
xtool dev build
xtool dev
```

To package a release IPA without waiting for a connected iPhone, run this from the same `ios` directory:

```bash
xtool dev build --configuration release --ipa
ls -lh xtool/BrakeCheck.ipa
```

The IPA is written to `ios/xtool/BrakeCheck.ipa`. `xtool dev` remains the connected-device signing and install flow.

The app requires iOS 17+. The bundle ID is `com.brakecheck.prototype`. If your signing setup needs a unique ID, change it in `ios/xtool.yml`. The renamed bundle installs separately from the old RoadWatch app, so old incidents are not migrated automatically; export any clips you need from the old app first.

No Swift or Apple SDK was available in this environment. The original Swift files were syntax parsed; the new pairing files still need an xtool/iPhone build. See `VALIDATION.md` for what was and was not checked.

## 4. Pair the iPhone

For a public HTTPS service, use the [Koyeb deployment guide](cloud/README.md) first, then run the same pairing utility with `--backend-url https://<your-koyeb-domain>`.

1. After updating an existing checkout, install the new backend packages once from `BrakeCheck\backend`:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   Put the iPhone and backend computer on the same reachable Wi-Fi. Keep the backend running on port 8000.
2. Open a second PowerShell window, go to `BrakeCheck\backend`, and run:

   ```powershell
   .\.venv\Scripts\python.exe pair.py
   ```

   A temporary QR image opens on the computer. It expires in five minutes and can pair one phone. Keep it visible only while pairing.
3. In BrakeCheck, open the sliders icon → **Connection** → **Scan pairing QR**. Allow Camera access; a same-Wi-Fi backend may also request Local Network access. The app exchanges the one-time code and saves a device token in the iPhone Keychain.
4. When BrakeCheck shows it is connected, return to PowerShell and press Enter to delete the temporary QR image. Future launches reconnect automatically. A LAN backend can be rediscovered through Bonjour if its computer's IP changes; a Koyeb backend reconnects over HTTPS. You can test the saved connection in Settings.
5. Tap **Prepare voice for this trip** and **Allow microphone for spoken replies**. When starting a trip, voice prefetch also runs in the background.

The QR contains a short-lived pairing code, not the permanent backend key. The pairing utility uses the backend admin key; the iPhone stores its own device token, and the server stores only a hash of that token. For a local server this is `backend/paired_devices.json`; on Koyeb it is `/data/paired_devices.json`. Revoke a paired phone through the authenticated `/pairing/devices/{device_id}` admin endpoint.

Windows Firewall may ask to allow Python on private networks; allow it for the network you trust. Campus Wi-Fi may isolate devices or block Bonjour. If pairing cannot discover a LAN backend, try a private hotspot or use **Advanced · manual connection** in Settings. Use the Koyeb guide for an internet-hosted HTTPS backend; do not expose the local development server directly to the public internet.

## 5. Add and confirm a contact

Open the people icon, enter a first name and an E.164 number (for example `+14045550123`), then tap **Add contact**. Tap **Invite** to open the share sheet and send the prepared invitation yourself. The contact opens the link and texts **YES** to their assigned Photon line. Tap **Refresh confirmations**, or return from Messages, to see “Ready for alerts.” Use **Test message** to send that confirmed contact a clearly marked test with no location; ask them to confirm it arrived. Photon acceptance is not a delivery receipt. A 60-second server cooldown limits accidental repeat sends.

Only contacts confirmed in this installation receive crash alerts. The old `EMERGENCY_CONTACTS` fallback has intentionally been removed so an unconfirmed or opted-out contact cannot be messaged through that fallback. STOP revokes alert access and access to location/status replies; YES restores it. If a remote deletion fails, local access is revoked and the contact stays visible so deletion can be retried.

This is a **single-driver backend**. Contacts and trip summaries are not separated into multiple accounts. Run separate backends/projects for independent drivers.

## 6. Demo, then enable a real check-in

**Safe rehearsal:** Settings → **Run crash check-in demo**. It exercises backend startup, ElevenLabs playback and transcription, and response/timeout handling with `dry_run=true`. It never contacts Photon to send a crash message, but ElevenLabs generation/transcription may use credits.

**Driving-metrics demo:** Settings → **Simulated driving metrics**. This is the original repeating simulated scenario; no sensors, recordings, crash messages, or trip summaries run from that mode.

**Live integration:** enable **Experimental impact detection** and **Start an emergency check-in on impact**. Both are off in a fresh installation. When an impact candidate arrives during a foreground live trip:

1. The phone marks available footage for preservation.
2. It sends recent coordinates, if available, and GPS speed to `/crash/start`.
3. It plays the check-in prompt. The microphone is never started until playback has ended.
4. `/arm` starts a 20-second response deadline. If allowed, the phone records six seconds and sends the reply to ElevenLabs through the backend. On-screen buttons remain available.
5. “I'm okay” suppresses the alert. Help or no response attempts a Photon message to confirmed contacts. If the phone disconnects before arming, the backend uses a 60-second deadline from session creation.
6. The app displays the actual server result, including failure or no confirmed contacts. Closing a disconnected check-in cannot cancel a server deadline; the screen explains this before dismissal.

The timer survives phone disconnection, **not backend shutdown**. Recognition uses conservative keyword rules, not a validated crash triage system. The impact sensor can mistake a phone drop for a crash or miss an impact. No emergency services are called automatically.

## 7. Footage and sensor behavior

The original 10-second rolling segments and requested 15-minute before/after preservation windows remain. Actual saved footage depends on how long the camera was running and whether it continues afterward. Locking the phone or backgrounding the app pauses video; no background microphone recording is implemented. Available incident clips can be viewed and shared manually from the film icon.

Clips are **not uploaded** by this merge. A Photon “clip” reply explains that available footage remains on the phone. Short microphone replies are temporary on iOS and are sent through your backend to ElevenLabs for transcription. Backend crash transcripts remain in process memory for this demo.

Live speed still comes from GPS. The experimental fused-speed display is in Diagnostics and is never fed into the risk estimator. Lead distance/speed/acceleration and TTC remain experimental camera estimates. Missing or stale readings display as unavailable; missing crash coordinates never become fake `0,0` coordinates.

## Tests

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

From `photon-agent/`:

```powershell
npm run typecheck
npm test
```

All these tests are offline and mock vendor calls. They do not start the Photon agent or send messages. Pure Swift core tests, on a machine with Swift 6:

```bash
swift test --package-path ios/Core --jobs 1 --disable-swift-testing
```

## Integration references

- [ElevenLabs speech creation](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- [ElevenLabs Scribe transcription](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [Photon iMessage connection and routing](https://photon.codes/docs/spectrum-ts/providers/imessage/connection-and-routing)
- [Apple AVAudioRecorder](https://developer.apple.com/documentation/avfaudio/avaudiorecorder)
- [xtool WSL/Linux setup](https://xtool.sh/documentation/xtool/installation-linux/)

Model provenance and third-party license notices are retained under `ios/`.
