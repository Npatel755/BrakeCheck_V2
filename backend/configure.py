"""Local setup; secrets are entered without echo and never written into source."""
from getpass import getpass
from pathlib import Path
import secrets
import os

path = Path(__file__).with_name('.env')
if path.exists():
    raise SystemExit('.env already exists. Edit it locally to update credentials.')
values = {
    'ELEVENLABS_API_KEY': getpass('ElevenLabs API key: ').strip(),
    'SPECTRUM_PROJECT_ID': input('Photon project ID: ').strip(),
    'SPECTRUM_PROJECT_SECRET': getpass('Photon project secret: ').strip(),
    'BRAKECHECK_APP_KEY': secrets.token_urlsafe(32),
    'AGENT_SHARED_KEY': secrets.token_urlsafe(32),
}
if any(not value or '\n' in value or '\r' in value for value in values.values()):
    raise SystemExit('Every credential is required and must be on a single line.')
content = Path(__file__).with_name('.env.example').read_text()
for key, value in values.items():
    content = content.replace(key + '=\n', key + '=' + value + '\n')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(content)
print('Created backend/.env. Keep BRAKECHECK_APP_KEY on this computer; run pair.py to display a temporary phone-pairing QR code.')
