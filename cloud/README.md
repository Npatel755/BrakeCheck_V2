# BrakeCheck on Koyeb

For Railway, use [the Railway setup guide](../README-RAILWAY.md).

This deploys the existing API and Photon messaging agent together as one Koyeb Web Service. Camera capture, Core ML vehicle detection, GPS sampling, and incident clips remain on the iPhone. The backend receives check-in data and sends contact messages; clips are not uploaded.

## Cloud and offline features

| Runs on the iPhone | Requires the cloud and internet |
| --- | --- |
| Camera, local vehicle detection, GPS sampling, rolling video, saved clips, driving interface, and check-in speech when on-device recognition supports the language | Pairing, remote check-in coordination, and emergency-contact messages through Photon |

The current Photon reply logic and spoken-reply classification use rules. This deployment does not add a new generative language model or GPU workload. ElevenLabs brake-warning audio is optional; device voice remains available without it. Photon and optional ElevenLabs usage may have separate provider charges.

## Prepare Koyeb

1. [Install and authenticate the Koyeb CLI](https://www.koyeb.com/docs/build-and-deploy/cli/installation). Alternatively, push this project to a private Git repository and configure the same Dockerfile and service settings in Koyeb.
2. In the Koyeb control panel, create these four required Secrets. Put each value directly into Koyeb; do not paste credentials into this guide, source files, or a public repository.

   | Secret name | Value to store |
   | --- | --- |
   | `brakecheck-app-key` | A new random app key, at least 24 characters |
   | `brakecheck-agent-key` | A separate new random agent key, at least 24 characters |
   | `spectrum-project-id` | Photon project ID |
   | `spectrum-project-secret` | Photon project secret |

   To use optional ElevenLabs brake audio, also create `elevenlabs-api-key` and deploy with `KOYEB_ENABLE_ELEVENLABS=1`. Otherwise the script does not require an ElevenLabs key.

   Create fresh BrakeCheck keys for this cloud service. If a credential was included in the original BrakeCheck ZIP, rotate it with its provider before entering it here.

3. Create a Koyeb Volume named `brakecheck-data` in the same region as the service. Check current volume availability, instance eligibility, and pricing in Koyeb before deploying, and keep a separate backup of data you need to preserve.
4. From this project directory, run:

   ```bash
   ./cloud/deploy-koyeb.sh
   ```

   The script stages only backend, agent, and deployment files, strips local `.env` files and user data from the staged archive, then deploys a single `small` instance with port 8000, HTTPS routing, `/healthz` checks, and the volume mounted at `/data`. It asks for the driver name before deploying. Use `KOYEB_REGION=fra` if the volume is in Frankfurt. You can choose other app/service or volume names with `KOYEB_APP_SERVICE` and `KOYEB_VOLUME`.

   The instance and settings are also available in Koyeb's UI. The service must expose HTTP port `8000`, route `/` to that port, use `GET /healthz` as its HTTP health check, mount the volume at `/data`, and stay at exactly one instance.

5. Check the deployment logs and open `https://<your-koyeb-domain>/healthz`. It should return `{"ok":true}`. Copy the assigned HTTPS service URL for phone pairing.

## Pair the iPhone

From `backend/`, run the pairing utility and pass the cloud URL:

```bash
python pair.py --backend-url https://<your-koyeb-domain>
```

Enter the Koyeb `BRAKECHECK_APP_KEY` when prompted. The utility makes a five-minute, one-use QR code; scan it in BrakeCheck under **Settings → Connection → Scan computer pairing QR**. The phone stores its device token in Keychain. The server stores only a hash on the `/data` volume.

After the contact replies YES and BrakeCheck shows **Ready for alerts**, open **Emergency contacts → Test message**. The app asks before sending a `BRAKECHECK TEST ONLY` message. It includes no coordinates or location, and the result means Photon accepted the send request; ask the contact to confirm arrival.

## Current prototype limits

- This is a single-driver prototype, not an emergency-service dispatch system. Impact detection and response rules are experimental; the app does not call emergency services.
- Active check-ins are stored on `/data` and recovered after a service restart. The agent's recent crash/trip context still lives in process memory. A prolonged outage may prevent timely contact delivery; check-ins overdue by more than five minutes are expired rather than sent as stale emergency alerts.
- Offline camera and detection do not make cloud contact alerts available offline. Photon messaging and cloud delivery require an internet connection. Spoken check-in replies can use on-device recognition when iOS supports the selected language; availability and accuracy vary by device and language.
- Use a service instance that supports the required `/data` volume, and verify current Koyeb plan, volume availability, and pricing before deployment. Keep a separate backup of persistent data.
