"""Opt-in exact HTTPS origins for the hosted UI; execution stays on loopback."""
import json
from pathlib import Path
import re
import time

def trusted_web_origin(origin, directory):
    if not isinstance(origin, str) or not re.fullmatch(r'https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', origin):
        return False
    path = Path(directory) / 'trusted-web-origins.json'
    try:
        if path.stat().st_size > 8192:
            return False
        config = json.loads(path.read_text())
        for item in config.get('origins', []):
            if not isinstance(item, dict) or item.get('origin') != origin:
                continue
            expires = item.get('expires_at')
            return expires is None or (type(expires) in (int, float) and time.time() < expires)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return False

def add_web_cors(response, request, directory):
    origin = request.headers.get('Origin')
    if (request.host.split(':')[0] in ('127.0.0.1', 'localhost')
            and trusted_web_origin(origin, directory)):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.vary.add('Origin')
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Publisher-Token'
        response.headers['Access-Control-Max-Age'] = '60'
        if request.headers.get('Access-Control-Request-Private-Network') == 'true':
            response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response
