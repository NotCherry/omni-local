"""Small, local-only implementation of llama.cpp's GA realtime wire protocol."""
import base64
import ipaddress
import json
from urllib.parse import urlsplit, urlencode

UPGRADE = ('Use a current ggml-org/llama.cpp build containing tools/server/server-realtime.cpp '
           'and /v1/realtime, with LFM3-Omni audio generation and its matching local mmproj. '
           'Start with --reasoning off. A chat/completions-only server is insufficient.')

def endpoint(value, model='default'):
    u = urlsplit(value.strip())
    if u.scheme not in ('http', 'ws') or u.username or u.password or u.query or u.fragment:
        raise ValueError('Use a plain local http://localhost:8080 URL, without credentials or query.')
    host = u.hostname
    try:
        local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == 'localhost'
    if not local:
        raise ValueError('Only loopback addresses are allowed: localhost, 127.0.0.1 or ::1.')
    if u.path.rstrip('/') not in ('', '/v1', '/v1/realtime'):
        raise ValueError('Expected server root, /v1, or /v1/realtime.')
    netloc = ('[' + host + ']' if ':' in host else host) + ':' + str(u.port or 8080)
    return 'ws://' + netloc + '/v1/realtime?' + urlencode({'model': model})

def encode(event):
    return json.dumps(event, separators=(',', ':'))

def append(pcm):
    if len(pcm) % 2:
        raise ValueError('PCM16 requires complete samples')
    return {'type': 'input_audio_buffer.append', 'audio': base64.b64encode(pcm).decode('ascii')}

def session_rates(event):
    if event.get('type') == 'error':
        raise ValueError(event.get('error', {}).get('message', 'Server rejected session') + '\n' + UPGRADE)
    if event.get('type') != 'session.created':
        raise ValueError('Expected session.created. ' + UPGRADE)
    session = event.get('session', {})
    if session.get('type') != 'realtime':
        raise ValueError('Server did not open a conversation session. ' + UPGRADE)
    try:
        formats = [session['audio'][x]['format'] for x in ('input', 'output')]
        if any(f['type'] != 'audio/pcm' for f in formats):
            raise ValueError('PCM16 audio required')
        rates = tuple(int(f['rate']) for f in formats)
        if any(r < 8000 or r > 192000 for r in rates):
            raise ValueError('Invalid sample rate')
        return rates
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError('Server session lacks usable native PCM audio input/output. ' + UPGRADE) from e

def session_update():
    return {'type': 'session.update', 'session': {'type': 'realtime',
            'instructions': 'Respond in interleaved text and audio.',
            'output_modalities': ['text', 'audio'], 'max_output_tokens': 1024}}

def audio_delta(event):
    pcm = base64.b64decode(event['delta'], validate=True)
    if len(pcm) % 2:
        raise ValueError('Server sent incomplete PCM16 samples')
    return pcm
