"""Local server discovery and explicitly configured local-file startup."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

def app_directory():
    return Path(sys.executable).resolve().parent/'OmniLocal' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent

def load_settings():
    try:
        return json.loads((app_directory()/'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

def save_settings(settings):
    folder = app_directory()
    folder.mkdir(exist_ok=True)
    (folder/'settings.json').write_text(json.dumps(settings, indent=2), encoding='utf-8')

def get_json(url, timeout=2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)

def inspect_server(port):
    url = f'http://127.0.0.1:{port}'
    try:
        models = get_json(url+'/v1/models', 1.5).get('data', [])
        found = []
        for model in models:
            architecture = model.get('architecture', {})
            native = 'audio' in architecture.get('input_modalities', []) and 'audio' in architecture.get('output_modalities', [])
            found.append({'url': url, 'model': model['id'], 'native_audio': native,
                          'description': 'native audio' if native else 'text-only or audio capability not advertised'})
        return found
    except Exception:
        return []

def discover():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = [m for group in pool.map(inspect_server, (8081,9931,8080,1234)) for m in group]
    return sorted(results, key=lambda m: (not m['native_audio'], 'omni' not in m['model'].lower()))

def server_arguments(settings):
    config = settings.get('local_server', {})
    executable = Path(os.path.expandvars(config.get('executable', '')))
    model = Path(os.path.expandvars(config.get('model', '')))
    projector = Path(os.path.expandvars(config.get('mmproj', '')))
    if not all(p.is_file() for p in (executable,model,projector)):
        raise ValueError('Local server setup needs existing llama executable, main GGUF and matching Omni audio projector. See settings.json and Setup report.')
    port = int(config.get('port', 8081))
    if not 1024 <= port <= 65535:
        raise ValueError('Invalid local server port')
    args = [str(executable)] + (['serve'] if executable.name.lower() == 'llama.exe' else [])
    args += ['-m', str(model), '--mmproj', str(projector), '--host','127.0.0.1','--port',str(port),
             '--alias','LFM3-Omni-Voice','--reasoning','off','--temp','0','-c','4096','-np','1',
             '-ngl',str(config.get('gpu_layers', 99))]
    if not config.get('projector_gpu', True):
        args += ['--no-mmproj-offload']
    return args, port

def start_server(settings, notify):
    args, port = server_arguments(settings)
    existing = inspect_server(port)
    if existing:
        if any(m['native_audio'] for m in existing):
            return f'http://127.0.0.1:{port}', next(m['model'] for m in existing if m['native_audio'])
        raise RuntimeError(f'Port {port} already serves a model without native audio. Choose a different port in settings.json.')
    logs = app_directory()/'logs'
    logs.mkdir(exist_ok=True)
    with (logs/'server.log').open('wb') as log:
        process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (logs/'server-pid.txt').write_text(str(process.pid))
    notify('status', 'Loading local Omni model and native audio projector…')
    deadline = time.monotonic()+150
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = (logs/'server.log').read_text(errors='replace')[-2200:]
            raise RuntimeError('Local server exited. '+tail)
        try:
            if get_json(f'http://127.0.0.1:{port}/health',1).get('status') == 'ok':
                return f'http://127.0.0.1:{port}', 'LFM3-Omni-Voice'
        except Exception:
            pass
        time.sleep(.5)
    raise RuntimeError('Server load exceeded 150 seconds; inspect OmniLocal/logs/server.log. Server remains running.')
