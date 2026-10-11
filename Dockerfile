FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRAKECHECK_DISABLE_MDNS=1 \
    PHOTON_AGENT_PORT=8787 \
    PHOTON_AGENT_URL=http://127.0.0.1:8787 \
    CONTACTS_FILE=/data/contacts.json \
    PAIRED_DEVICES_FILE=/data/paired_devices.json \
    VOICE_CACHE_DIR=/data/voice_cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python3 -m venv /opt/brakecheck-venv \
    && /opt/brakecheck-venv/bin/pip install --no-cache-dir -r /app/backend/requirements.txt
ENV PATH="/opt/brakecheck-venv/bin:${PATH}" \
    PYTHONPATH=/app/backend

COPY backend/ /app/backend/
COPY photon-agent/ /app/photon-agent/
RUN cd /app/photon-agent && npm ci --omit=dev --no-audit --no-fund

COPY cloud/ /app/cloud/
RUN mkdir -p /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "/app/cloud/healthcheck.py"]

ENTRYPOINT ["node", "/app/cloud/start.mjs"]
