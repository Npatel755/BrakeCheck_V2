"""Private remote-review link configuration; no keys are served anonymously."""
import hashlib,json,re,secrets
from urllib.parse import urlsplit
from .bootstrap import atomic_json

def load_remote(settings):
    path=settings.path('reports','remote_access.json')
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    if not isinstance(data,dict) or data.get('enabled') is not True:return {}
    url=data.get('url');key=data.get('access_key')
    if not isinstance(url,str) or not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,128}',key):return {}
    try:
        parts=urlsplit(url);host=parts.hostname or ''
        if (parts.scheme!='https' or parts.username is not None or parts.password is not None
                or parts.port not in (None,443) or parts.path not in ('','/') or parts.query or parts.fragment):return {}
    except ValueError:return {}
    if not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com',host):return {}
    return {**data,'host':host,'review_url':data['url'].rstrip('/')+'/#access='+data['access_key']}

def rotate_remote(settings):
    data=load_remote(settings)
    if not data:return {}
    data.pop('host',None);data.pop('review_url',None)
    data['access_key']=secrets.token_urlsafe(32)
    atomic_json(settings.path('reports','remote_access.json'),data)
    return load_remote(settings)

def valid_key(settings,key):
    data=load_remote(settings)
    return bool(data) and secrets.compare_digest(hashlib.sha256(key.encode()).digest(),hashlib.sha256(data['access_key'].encode()).digest())
