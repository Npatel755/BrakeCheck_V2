#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
staging_root="$(mktemp -d)"
trap 'rm -rf "$staging_root"' EXIT

cp -a "$project_root/Dockerfile" "$project_root/.dockerignore" "$staging_root/"
cp -a "$project_root/backend" "$project_root/photon-agent" "$project_root/cloud" "$staging_root/"

# Keep app credentials, user data, local dependencies, and device builds out of
# Koyeb's source archive as well as out of the Docker image.
find "$staging_root" -type f \( -name '.env' -o -name '.env.*' \) ! -name '.env.example' -delete
find "$staging_root" -type f \( -name 'contacts.json' -o -name 'contacts.tmp' \
  -o -name 'paired_devices.json' -o -name 'paired_devices.tmp' -o -name '*.ipa' \) -delete
find "$staging_root" -type d \( -name '.venv' -o -name 'node_modules' -o -name '__pycache__' \
  -o -name '.pytest_cache' -o -name 'voice_cache' -o -name 'voice_samples' \) -prune -exec rm -rf {} +

if ! command -v koyeb >/dev/null 2>&1; then
  echo "Install and authenticate the Koyeb CLI, then run this script again." >&2
  exit 1
fi

read -r -p 'Driver name shown in contact messages: ' driver_name
if [[ -z "${driver_name//[[:space:]]/}" ]]; then
  echo 'Enter a driver name.' >&2
  exit 1
fi

app_service="${KOYEB_APP_SERVICE:-brakecheck/api}"
volume_name="${KOYEB_VOLUME:-brakecheck-data}"
region="${KOYEB_REGION:-was}"

koyeb deploy "$staging_root" "$app_service" \
  --archive-builder docker \
  --instance-type small \
  --scale 1 \
  --regions "$region" \
  --ports 8000:http \
  --routes /:8000 \
  --checks 8000:http:/healthz \
  --volumes "$volume_name:/data" \
  --env 'BRAKECHECK_APP_KEY={{secret.brakecheck-app-key}}' \
  --env 'AGENT_SHARED_KEY={{secret.brakecheck-agent-key}}' \
  --env 'SPECTRUM_PROJECT_ID={{secret.spectrum-project-id}}' \
  --env 'SPECTRUM_PROJECT_SECRET={{secret.spectrum-project-secret}}' \
  --env 'ELEVENLABS_API_KEY={{secret.elevenlabs-api-key}}' \
  --env "DRIVER_NAME=$driver_name" \
  --env BRAKECHECK_DISABLE_MDNS=1 \
  --env PHOTON_AGENT_PORT=8787 \
  --env BACKEND_URL=http://127.0.0.1:8000 \
  --env PHOTON_AGENT_URL=http://127.0.0.1:8787 \
  --env CONTACTS_FILE=/data/contacts.json \
  --env PAIRED_DEVICES_FILE=/data/paired_devices.json \
  --env VOICE_CACHE_DIR=/data/voice_cache \
  --wait
