# BrakeCheck on Railway — Windows setup

This package is prepared for Railway. Deployment still needs your Railway login,
service variables, and a volume in your account. One Docker service runs both
the FastAPI backend and Photon messaging agent. Vehicle detection, GPS, and
video recording run on the iPhone; Railway does not need a GPU.

## 1. Extract and create the service

Extract the ZIP and open the inner `BrakeCheck` folder in VS Code. Open a
PowerShell terminal. It must show `Dockerfile`, `railway.json`, `backend`, and
`photon-agent` in the current folder. Deploy from here, not from `backend`.

With Node.js and Python installed, run these commands one at a time:

```powershell
npm.cmd install --global @railway/cli
railway.cmd login
railway.cmd init
railway.cmd add --service brakecheck
railway.cmd open
```

Complete the browser login. For `init`, choose your personal workspace and
name the project `BrakeCheck`. Creating the empty service links this folder
to it. The final command opens that project in your browser. The `.cmd`
commands use npm's Windows launchers and work in PowerShell without changing
its script execution policy. On macOS/Linux, use `npm` and `railway` instead.

If you already created the intended project, use `railway.cmd link` instead
of `init`. Choose the existing service if it exists; otherwise add `brakecheck`.

## 2. Add the service variables

Generate two separate random keys on your computer:

```powershell
py -c "import secrets; print('BRAKECHECK_APP_KEY=' + secrets.token_urlsafe(32)); print('AGENT_SHARED_KEY=' + secrets.token_urlsafe(32))"
```

In Railway, click the **brakecheck** service, then **Variables → RAW Editor**.
Paste the contents of `cloud/railway-variables.env.example` and replace every
`CHANGE_ME` placeholder before saving:

| Variable | Value |
| --- | --- |
| `BRAKECHECK_APP_KEY` | First generated key; keep it for the QR pairing command |
| `AGENT_SHARED_KEY` | Second generated key, different from the app key |
| `SPECTRUM_PROJECT_ID` | Your Photon project ID |
| `SPECTRUM_PROJECT_SECRET` | Your Photon project secret |
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key |
| `DRIVER_NAME` | The driver's name as it should appear in contact messages |
| `PORT` | `8000` |
| `PHOTON_AGENT_PORT` | `8787` |
| `BRAKECHECK_DISABLE_MDNS` | `1` |
| `CONTACTS_FILE` | `/data/contacts.json` |
| `PAIRED_DEVICES_FILE` | `/data/paired_devices.json` |
| `VOICE_CACHE_DIR` | `/data/voice_cache` |

Enter credentials directly in Railway. Do not share screenshots with values
visible. This ZIP contains placeholders, not live credentials.

Leave `BACKEND_URL` unset so the launcher derives it from `PORT`. The internal
agent URL already defaults to `http://127.0.0.1:8787`. Neither internal URL
should be set to the public Railway URL. Keep any custom ElevenLabs voice IDs
or model settings you already use by adding the corresponding variables too.

Review and apply staged variable changes. The Photon project credentials and
agent key are required: the existing agent exits if they are missing, and the
launcher then stops the API too.

## 3. Attach persistent storage

Right-click the Railway project canvas and add a **Volume**. Attach it to
**brakecheck** and set its mount path to:

```text
/data
```

Use the available 0.5 GB trial/free volume allocation. This preserves paired
device hashes, contacts, and cached voice files between deployments. Without
the volume, replacing the container can erase that data. The volume consumes
usage credit too; watch the Railway Usage page.

Keep exactly **one replica** and leave **Serverless/app sleeping disabled** for
the demo: crash countdowns and recent trip state are currently held in memory.
A restart still interrupts an active countdown even with persistent storage.
The first cloud deployment starts with a fresh contact list; local contacts
are not copied by deployment.

## 4. Deploy

Before switching, stop the old Photon agent on your computer, or coordinate
with the teammate running it. Use one active agent for this Photon project to
avoid duplicate replies. No contact messages are sent by the setup commands
themselves; the running agent will handle incoming messages for its project.

From the same `BrakeCheck` folder:

```powershell
railway.cmd up
```

Railway builds the Docker image. Docker does not need to be installed on your
Windows computer. Keep the service's custom start command blank: the Docker
entrypoint starts both processes. `railway.json` selects the Dockerfile and
checks `/healthz`. The launcher uses a single Uvicorn worker.

After it becomes healthy, go to the service's **Settings → Networking → Public
Networking → Generate Domain**. If asked for a target port, enter **8000**.
Copy the HTTPS base URL and open:

```text
https://YOUR-RAILWAY-DOMAIN/healthz
```

Expected response: `{"ok":true}`. A 404 at `/` is normal; this is an API, with no
homepage. `/healthz` confirms the API responds, not that messages or voice have
been tested end to end. Inspect logs for both the Uvicorn startup and the
Photon agent listening on `127.0.0.1:8787`.

## 5. Pair the iPhone

From the same root folder, create a local environment for the QR utility if
you do not already have one:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe backend\pair.py --backend-url https://YOUR-RAILWAY-DOMAIN
```

Replace `https://YOUR-RAILWAY-DOMAIN` with your actual HTTPS base URL, with no
`/healthz` suffix and no `:8000`. When prompted, paste the **Railway**
`BRAKECHECK_APP_KEY`; input is hidden. On Windows the QR image opens automatically.
Keep the terminal waiting until the phone has scanned it.

In BrakeCheck, open **Settings → Connection → Scan computer pairing QR**.
Scan the new cloud QR within five minutes. It contains the cloud address and
a one-use pairing code, so you do not need to type the address or key on the
phone. This source version already supports HTTPS URLs; no Swift changes were
required for Railway. If your installed build is older, it must have the full
QR-payload scanner from this project's iOS source.

Re-add your emergency contacts to the new cloud backend and have them complete
the existing YES opt-in flow. Only initiate the app's **Test message** action
when you intend to send that contact its clearly labeled test message.

To check remote access, switch the phone from Wi-Fi to cellular and use the
app's connection check. After cloud pairing succeeds, your PC does not need
to keep Uvicorn or the Photon agent running.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `npm.cmd` is not recognized | Install Node.js, reopen VS Code, and retry |
| Docker cannot find `backend/requirements.txt` | Deploy from the inner `BrakeCheck` directory containing the Dockerfile |
| Agent says to run `configure.py` | Set the Photon project ID, secret, and a 24+ character agent key in Railway Variables; local `.env` files are intentionally excluded |
| Bad gateway or healthcheck failure | Check deployment logs, `PORT=8000`, domain target port 8000, and a blank custom start command |
| Pairing returns 401 | Use the cloud app key, and issue a fresh QR if its code expired or was already used |
| Phone loses pairing after a deployment | Confirm a volume is attached at `/data` |
| Contact replies do not update status | Check Photon connectivity and ensure `BACKEND_URL` was not manually set to the wrong port |
| Photon or ElevenLabs cannot connect | Check credentials, provider status, and any outbound restrictions on a limited Railway trial |
| Process exits from memory pressure | Inspect Railway metrics; both Python and Node share the plan's memory allocation |

## Included deployment changes

- Added Railway Docker and healthcheck configuration and an upload ignore file.
- Removed the Docker image's fixed `BACKEND_URL`. The existing launcher now
  correctly supplies the API URL if a cloud platform selects a different port.
- Changed the pairing utility's Koyeb-specific wording to cloud wording.
- Added the variables template and this Windows setup guide.

## Official references

Verification for this update: Python syntax and Railway JSON checked; the
launcher was exercised with mocked child processes for default, platform-set,
and explicitly overridden ports, including shutdown. The iOS source matches
the uploaded version. A live Docker build, Railway deployment, and provider
connectivity have not been run in this environment; complete the deployment
and phone checks above in your account.

Checked October 10, 2026:

- [Railway CLI installation and login](https://docs.railway.com/cli)
- [Create an empty service](https://docs.railway.com/cli/add)
- [CLI uploads and ignore files](https://docs.railway.com/cli/up)
- [Service variables](https://docs.railway.com/variables)
- [Persistent volumes](https://docs.railway.com/volumes)
- [Public networking](https://docs.railway.com/networking/public-networking)
- [Railway deployment configuration](https://docs.railway.com/config-as-code/reference)
- [Free trial and network restrictions](https://docs.railway.com/pricing/free-trial)
- [Plan resource limits](https://railway.com/pricing)
