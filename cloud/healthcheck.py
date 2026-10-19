import os
from urllib.request import urlopen

port = os.getenv('PORT', '8000')
with urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
