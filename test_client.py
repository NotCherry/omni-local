import base64
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.sync.server import serve
from audio import Playback, EnergyVAD
from protocol import endpoint, append, session_rates, audio_delta
from client import VoiceClient
from server_manager import server_arguments, inspect_server

SESSION = {'type': 'session.created', 'session': {'type': 'realtime', 'model': 'LFM3-Omni mock',
           'audio': {'input': {'format': {'type': 'audio/pcm', 'rate': 16000}, 'turn_detection': None},
                     'output': {'format': {'type': 'audio/pcm', 'rate': 24000}}}}}

class ProtocolTests(unittest.TestCase):
    def test_local_urls(self):
        for url in ['http://localhost:8080', 'http://127.0.0.1:8080/v1', 'ws://localhost:8080/v1/realtime']:
            self.assertIn('/v1/realtime?model=default', endpoint(url))
        self.assertIn('[::1]:8080', endpoint('http://[::1]:8080'))
        for url in ['https://example.com', 'http://example.com', 'http://127.0.0.1.evil.test',
                    'http://user@localhost', 'http://localhost/other', 'http://localhost?x=1']:
            with self.assertRaises(ValueError):
                endpoint(url)

    def test_pcm_and_rates(self):
        pcm = b'\x01\x00' * 320
        self.assertEqual(base64.b64decode(append(pcm)['audio']), pcm)
        self.assertEqual(audio_delta({'delta': append(pcm)['audio']}), pcm)
        self.assertEqual(session_rates(SESSION), (16000, 24000))
        for bad in [{'type': 'hello'}, {'type': 'error', 'error': {'message': 'bad model'}},
                    {'type': 'session.created', 'session': {'type': 'realtime', 'audio': {}}}]:
            with self.assertRaises(ValueError):
                session_rates(bad)
        with self.assertRaises(ValueError):
            append(b'x')
        with self.assertRaises(ValueError):
            audio_delta({'delta': 'eA=='})

    def test_playback_buffer_tail_mute_and_bound(self):
        p = Playback(16000, 100)
        p.add(b'\x01\x00' * 320)
        out = bytearray(640)
        p.callback(out, 320, None, None)
        self.assertEqual(out, bytes(640))  # waits for prebuffer
        p.finished = True
        p.callback(out, 320, None, None)
        self.assertEqual(out, b'\x01\x00' * 320)
        self.assertEqual(p.pending(), 0)
        with self.assertRaises(RuntimeError):
            p.add(bytes(16000*2*31))
        p.muted = True
        p.add(b'\x01\x00' * 320)
        self.assertEqual(p.pending(), 0)

    def test_vad_onset_preroll_silence(self):
        v = EnergyVAD(450)
        silence, voice = bytes(640), b'\x10\x27' * 320
        for _ in range(10):
            self.assertEqual(v.feed(silence), (b'', False))
        self.assertEqual(v.feed(voice), (b'', False))
        self.assertEqual(v.feed(voice), (b'', False))
        pcm, ended = v.feed(voice)
        self.assertEqual(len(pcm), 6400)
        self.assertFalse(ended)
        for _ in range(34):
            self.assertFalse(v.feed(silence)[1])
        self.assertTrue(v.feed(silence)[1])
        self.assertFalse(v.active)

class DummyStream:
    def __init__(self, *args, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

class SocketTests(unittest.TestCase):
    def wait_event(self, events, wanted, timeout=4):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            kind, value = events.get(timeout=max(0.01, deadline-time.monotonic()))
            seen.append((kind, value))
            if kind == wanted:
                return value
            if kind == 'error' and wanted != 'error':
                self.fail(str(value))
        self.fail(f'Missing {wanted}: {seen}')

    def test_real_websocket_turn_and_cancellation(self):
        received = []
        def handler(ws):
            ws.send(json.dumps(SESSION))
            update = json.loads(ws.recv(timeout=3))
            received.append(update)
            ws.send(json.dumps({'type': 'session.updated', 'session': SESSION['session']}))
            while True:
                try:
                    ev = json.loads(ws.recv(timeout=4))
                except Exception:
                    return
                received.append(ev)
                if ev['type'] == 'response.create':
                    ws.send(json.dumps({'type': 'response.output_text.delta', 'delta': 'Hello locally'}))
                    ws.send(json.dumps({'type': 'response.output_audio.delta', 'delta': base64.b64encode(bytes(960)).decode()}))
                elif ev['type'] == 'response.cancel':
                    # Late audio after cancellation must be discarded by client.
                    ws.send(json.dumps({'type': 'response.output_audio.delta', 'delta': base64.b64encode(bytes(960)).decode()}))
                    ws.send(json.dumps({'type': 'response.done', 'response': {'status': 'cancelled'}}))
        with serve(handler, '127.0.0.1', 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            events = queue.Queue()
            c = VoiceClient(lambda k, v: events.put((k, v)))
            with patch('client.sd.RawInputStream', DummyStream), patch('client.sd.RawOutputStream', DummyStream), \
                 patch('client.sd.check_input_settings'), patch('client.sd.check_output_settings'):
                try:
                    c.start(f'http://127.0.0.1:{server.socket.getsockname()[1]}', None, None, 'Push to talk', 450, 100)
                    self.wait_event(events, 'ready')
                    c.command('begin')
                    c.command('pcm', b'\x01\x00'*320)
                    c.command('end')
                    self.assertEqual(self.wait_event(events, 'text'), 'Hello locally')
                    self.wait_event(events, 'latency')
                    c.command('cancel')
                    self.assertEqual(self.wait_event(events, 'done'), 'cancelled')
                    self.assertEqual(c.playback.pending(), 0)
                    types = [e['type'] for e in received]
                    self.assertEqual(types, ['session.update', 'input_audio_buffer.append', 'response.create', 'response.cancel'])
                    self.assertEqual(received[0]['session']['instructions'], 'Respond in interleaved text and audio.')
                finally:
                    c.disconnect()
                    c.thread.join(4)
                    server.shutdown()
                self.assertFalse(c.thread.is_alive())

    def test_unsupported_endpoint_actionable(self):
        from http import HTTPStatus
        def reject(conn, request):
            return conn.respond(HTTPStatus.NOT_FOUND, 'No realtime support')
        with serve(lambda ws: None, '127.0.0.1', 0, process_request=reject) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            events = queue.Queue()
            c = VoiceClient(lambda k, v: events.put((k, v)))
            c.start(f'http://127.0.0.1:{server.socket.getsockname()[1]}', None, None, 'Push to talk', 450, 100)
            error = self.wait_event(events, 'error')
            self.assertIn('HTTP 404', error)
            self.assertIn('server-realtime.cpp', error)
            c.thread.join(3)
            server.shutdown()

    def test_automatic_vad_turn_and_server_error_cleanup(self):
        received = []
        def handler(ws):
            ws.send(json.dumps(SESSION))
            received.append(json.loads(ws.recv(timeout=3)))
            ws.send(json.dumps({'type': 'session.updated'}))
            while True:
                try:
                    ev = json.loads(ws.recv(timeout=3))
                except Exception:
                    return
                received.append(ev)
                if ev['type'] == 'response.create':
                    ws.send(json.dumps({'type': 'error', 'error': {'message': 'mock GPU out of memory'}}))
                    return
        with serve(handler, '127.0.0.1', 0) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            events = queue.Queue()
            c = VoiceClient(lambda k,v: events.put((k,v)))
            with patch('client.sd.RawInputStream', DummyStream), patch('client.sd.RawOutputStream', DummyStream), \
                 patch('client.sd.check_input_settings'), patch('client.sd.check_output_settings'):
                c.start(f'http://127.0.0.1:{server.socket.getsockname()[1]}', None, None,
                        'Automatic • local energy VAD', 450, 100)
                self.wait_event(events, 'ready')
                # 60 ms onset followed by 700 ms silence must end exactly one turn.
                for _ in range(3):
                    c.command('pcm', b'\x10\x27'*320)
                for _ in range(35):
                    c.command('pcm', bytes(640))
                self.assertIn('mock GPU out of memory', self.wait_event(events, 'error'))
                self.wait_event(events, 'disconnected')
                c.thread.join(3)
                self.assertFalse(c.capture)
                self.assertEqual(sum(e['type'] == 'response.create' for e in received), 1)
                self.assertEqual(sum(e['type'] == 'input_audio_buffer.append' for e in received), 36)
                self.assertEqual(c.playback.pending(), 0)
            server.shutdown()

    def test_audio_device_rejection_disconnects(self):
        def handler(ws):
            ws.send(json.dumps(SESSION))
            ws.recv(timeout=3)
            ws.send(json.dumps({'type': 'session.updated'}))
            try:
                ws.recv(timeout=3)
            except Exception:
                pass
        with serve(handler, '127.0.0.1', 0) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            events = queue.Queue()
            c = VoiceClient(lambda k,v: events.put((k,v)))
            with patch('client.sd.check_input_settings', side_effect=ValueError('unsupported sample rate')):
                c.start(f'http://127.0.0.1:{server.socket.getsockname()[1]}', None, None, 'Push to talk', 450, 100)
                self.assertIn('unsupported sample rate', self.wait_event(events, 'error'))
                c.thread.join(3)
                self.assertFalse(c.capture)
            server.shutdown()

@unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell startup scripts only run on Windows')
class WindowsScriptsTests(unittest.TestCase):
    def test_managed_llama_command_and_missing_projector(self):
        with tempfile.TemporaryDirectory(prefix='managed voice ') as folder:
            paths = [Path(folder)/name for name in ('llama.exe','model file.gguf','audio projector.gguf')]
            for path in paths:
                path.touch()
            config={'local_server':{'executable':str(paths[0]),'model':str(paths[1]),'mmproj':str(paths[2]),'port':8081}}
            args,port=server_arguments(config)
            self.assertEqual(args[:2],[str(paths[0]),'serve'])
            self.assertEqual(args[args.index('-m')+1],str(paths[1]))
            self.assertEqual(args[args.index('--mmproj')+1],str(paths[2]))
            self.assertEqual(args[args.index('--host')+1],'127.0.0.1')
            self.assertEqual(port,8081)
            self.assertNotIn('-hf',args)
            paths[2].unlink()
            with self.assertRaisesRegex(ValueError,'projector'):
                server_arguments(config)

    def test_server_discovery_distinguishes_text_only(self):
        models={'data':[{'id':'text-only','architecture':{'input_modalities':['text'],'output_modalities':['text']}},
                        {'id':'omni-voice','architecture':{'input_modalities':['text','audio'],'output_modalities':['text','audio']}}]}
        with patch('server_manager.get_json',return_value=models):
            result=inspect_server(9931)
        self.assertFalse(result[0]['native_audio'])
        self.assertTrue(result[1]['native_audio'])
        self.assertEqual(result[1]['url'],'http://127.0.0.1:9931')

    def test_exact_startup_args_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='omni test ') as folder:
            paths = [Path(folder) / x for x in ['llama server.exe', 'local model.gguf', 'local projector.gguf']]
            for p in paths:
                p.touch()
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                  '-File', str(Path(__file__).parent/'Start-Server.ps1'), '-Server', str(paths[0]),
                  '-Model', str(paths[1]), '-Mmproj', str(paths[2]), '-CheckOnly'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            obj = json.loads(result.stdout[result.stdout.index('{'):])
            self.assertEqual(obj['arguments'], ['-m', str(paths[1]), '--mmproj', str(paths[2]),
                    '--host', '127.0.0.1', '--port', '8080', '--reasoning', 'off', '-ngl', '99', '-c', '4096'])
            self.assertNotIn('-hf', obj['arguments'])

    def test_missing_files_fail_before_launch(self):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
              '-File', str(Path(__file__).parent/'Start-Server.ps1'), '-Server', 'missing.exe',
              '-Model', 'missing.gguf', '-Mmproj', 'missing.gguf', '-CheckOnly'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Local file not found', result.stderr)

if __name__ == '__main__':
    unittest.main(verbosity=2)
