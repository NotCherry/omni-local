import audioop
import json
import queue
import threading
import time
import urllib.request
from contextlib import ExitStack

import sounddevice as sd
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus

from audio import Playback, EnergyVAD
from protocol import endpoint, encode, append, session_rates, session_update, audio_delta, UPGRADE

class VoiceClient:
    def __init__(self, notify):
        self.notify = notify
        self.commands = queue.Queue(maxsize=250)  # at most five seconds of mic data
        self.stop = threading.Event()
        self.capture = False
        self.paused = False
        self.playback = None
        self.thread = None
        self.dropped = 0

    def command(self, name, value=None):
        try:
            self.commands.put_nowait((name, value))
        except queue.Full:
            self.stop.set()
            self.notify('error', 'Input queue overflow. Disconnected to avoid sending a damaged turn.')

    def start(self, url, input_device, output_device, mode, threshold, buffer_ms, model='default'):
        if self.thread and self.thread.is_alive():
            raise RuntimeError('Previous connection is still closing')
        address = endpoint(url, model)
        self.stop.clear()
        self.capture = self.paused = False
        self.commands = queue.Queue(maxsize=250)
        self.thread = threading.Thread(target=self.run, args=(address, input_device, output_device,
                      mode, threshold, buffer_ms), daemon=True)
        self.thread.start()

    def disconnect(self):
        self.capture = False
        self.stop.set()

    def run(self, address, input_device, output_device, mode, threshold, buffer_ms):
        self.playback = None
        try:
            self.notify('status', 'Connecting to local realtime endpoint…')
            # Explicit proxy=None prevents environment proxies from receiving local audio.
            with connect(address, open_timeout=6, close_timeout=1, max_size=16*1024*1024,
                         proxy=None) as ws, ExitStack() as stack:
                first = json.loads(ws.recv(timeout=8))
                in_rate, out_rate = session_rates(first)
                ws.send(encode(session_update()))
                updated = json.loads(ws.recv(timeout=8))
                if updated.get('type') == 'error':
                    raise RuntimeError(updated.get('error', {}).get('message', 'Session rejected'))
                if updated.get('type') != 'session.updated':
                    raise RuntimeError('Expected session.updated; server protocol differs. ' + UPGRADE)
                self.playback = Playback(out_rate, buffer_ms)
                sd.check_input_settings(device=input_device, channels=1, dtype='int16', samplerate=in_rate)
                sd.check_output_settings(device=output_device, channels=1, dtype='int16', samplerate=out_rate)
                def mic(data, frames, timing, status):
                    if status:
                        self.dropped += 1
                    if self.capture and not self.paused:
                        self.command('pcm', bytes(data))
                stack.enter_context(sd.RawOutputStream(device=output_device, samplerate=out_rate,
                    channels=1, dtype='int16', blocksize=max(1, out_rate//50),
                    callback=self.playback.callback))
                stack.enter_context(sd.RawInputStream(device=input_device, samplerate=in_rate,
                    channels=1, dtype='int16', blocksize=max(1, in_rate//50), callback=mic))
                self.notify('ready', f"{first['session'].get('model', 'Local model')}  •  {in_rate} → {out_rate} Hz PCM16")
                vad = EnergyVAD(threshold)
                responding = sent = holding = cancelling = False
                ended_at = first_audio = None
                last_stats = 0
                def send_pcm(pcm):
                    nonlocal sent
                    ws.send(encode(append(pcm)))
                    sent = True
                def finish():
                    nonlocal sent, responding, ended_at, first_audio, holding
                    holding = False
                    if sent:
                        with self.playback.lock:
                            self.playback.finished = False
                        ws.send(encode({'type': 'response.create'}))
                        sent = False
                        responding = True
                        ended_at = time.perf_counter()
                        first_audio = None
                        self.notify('turn', 'You • audio turn sent (input transcript may be unavailable)')
                        self.notify('status', 'Generating native model speech…')
                while not self.stop.is_set():
                    busy = responding or self.playback.pending() > 0
                    self.capture = not self.paused and not busy and (mode == 'Automatic • local energy VAD' or holding)
                    for _ in range(50):
                        try:
                            name, value = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        if name == 'begin' and not responding and self.playback.pending() == 0 and not self.paused:
                            holding = True
                            self.capture = True
                            self.notify('status', 'Listening… release to send')
                        elif name == 'end':
                            self.capture = False
                            finish()
                        elif name == 'begin':
                            self.notify('status', 'Wait for playback, or stop the current response first.')
                        elif name == 'pause':
                            self.paused = bool(value)
                            self.capture = False
                            if self.paused:
                                finish()  # already-prefilled audio cannot be discarded upstream
                                vad.reset()
                            self.notify('status', 'Microphone paused' if self.paused else 'Ready')
                        elif name == 'mute':
                            with self.playback.lock:
                                self.playback.muted = bool(value)
                                if value:
                                    self.playback.data.clear()
                        elif name == 'cancel':
                            self.playback.clear()
                            if responding:
                                cancelling = True
                                ws.send(encode({'type': 'response.cancel'}))
                            self.notify('status', 'Playback cleared; cancellation requested' if responding else 'Playback cleared')
                        elif name == 'pcm' and not responding and not self.paused:
                            self.notify('level', min(100, audioop.rms(value, 2) / 60))
                            if mode == 'Automatic • local energy VAD':
                                pcm, end = vad.feed(value)
                                if pcm:
                                    send_pcm(pcm)
                                if end:
                                    finish()
                            else:
                                send_pcm(value)
                    try:
                        ev = json.loads(ws.recv(timeout=0.01))
                    except TimeoutError:
                        ev = {}
                    kind = ev.get('type')
                    if kind == 'response.output_audio.delta' and not cancelling:
                        if first_audio is None and ended_at is not None:
                            first_audio = time.perf_counter()
                            self.notify('latency', (first_audio - ended_at)*1000)
                        self.playback.add(audio_delta(ev))
                    elif kind == 'response.output_text.delta':
                        self.notify('text', ev.get('delta', ''))
                    elif kind == 'conversation.item.input_audio_transcription.delta':
                        self.notify('transcript', ev)
                    elif kind == 'conversation.item.input_audio_transcription.completed':
                        self.notify('transcript', ev)
                    elif kind == 'response.done':
                        responding = cancelling = False
                        with self.playback.lock:
                            self.playback.finished = True
                        self.notify('done', ev.get('response', {}).get('status', 'done'))
                        self.notify('status', 'Ready • listening resumes after playback' if not self.paused else 'Microphone paused')
                        if first_audio is None and ev.get('response', {}).get('status') == 'completed':
                            self.notify('notice', 'Response ended without speech. Check native audio model/sidecars and server log.')
                    elif kind == 'error':
                        # Fatal session reset avoids ambiguous server state after a rejected turn.
                        raise RuntimeError(ev.get('error', {}).get('message', str(ev)))
                    now = time.perf_counter()
                    if now - last_stats > 0.25:
                        last_stats = now
                        self.notify('stats', (self.playback.pending(), self.playback.underruns, self.dropped,
                                             self.capture, responding))
                    if ended_at and responding and now - ended_at > 120:
                        raise TimeoutError('No completed response within 120 s. Inspect server GPU/memory/context logs.')
        except InvalidStatus as e:
            self.notify('error', f'Realtime WebSocket rejected: HTTP {e.response.status_code}. {UPGRADE}')
        except Exception as e:
            self.notify('error', f'{type(e).__name__}: {e}\nCheck local server address, Windows microphone permission, '
                        'device sample-rate support, matching model sidecars and server logs. ' + UPGRADE)
        finally:
            self.capture = False
            if self.playback:
                self.playback.clear()
            self.notify('disconnected', None)

def health(url):
    address = endpoint(url)
    from urllib.parse import urlsplit
    u = urlsplit(address)
    req = urllib.request.Request('http://' + u.netloc + '/health')
    # No system proxy for local health requests either.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=4) as r:
        return r.status, r.read(4096).decode('utf-8', errors='replace')
