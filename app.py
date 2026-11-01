"""Omni Local — Windows native-audio conversation client."""
import argparse
import ctypes
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import sounddevice as sd
from client import VoiceClient, health
from server_manager import load_settings, save_settings, discover, start_server

def create_root():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    # Keep the compact controls readable without virtualization/clipping at 150% Windows scale.
    root.tk.call('tk', 'scaling', 4/3)
    return root

class App:
    def __init__(self, root):
        self.root = root
        root.title('Omni Local • native voice')
        root.geometry(f'960x{min(790, root.winfo_screenheight()-90)}+60+40')
        root.minsize(800, 640)
        root.configure(bg='#101820')
        self.events = queue.Queue()
        self.client = VoiceClient(lambda kind, value: self.events.put((kind, value)))
        self.connected = self.pressed = self.assistant_open = False
        self.transcripts = {}
        self.settings = load_settings()
        self.url = tk.StringVar(value=self.settings.get('url','http://localhost:8080'))
        self.model = tk.StringVar(value=self.settings.get('model','default'))
        self.mode = tk.StringVar(value='Push to talk')
        self.paused = tk.BooleanVar()
        self.muted = tk.BooleanVar()
        self.threshold = tk.IntVar(value=450)
        self.buffer = tk.IntVar(value=self.settings.get('buffer_ms',100))
        self.status = tk.StringVar(value='Connect to an existing local llama-server to begin.')
        self.details = tk.StringVar(value='No model downloads • no cloud • no Whisper or external TTS')
        self.metrics = tk.StringVar(value='First audio: —   |   Buffered: 0 ms   |   Underruns: 0')
        self.latency = None
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', background='#101820', foreground='#e7eef4', font=('Segoe UI', 10))
        style.configure('TFrame', background='#101820')
        style.configure('TLabel', background='#101820')
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 24), foreground='#71e4c2')
        style.configure('TButton', background='#263b49', padding=(14, 9))
        style.map('TButton', background=[('active', '#365566')])
        style.configure('Talk.TButton', font=('Segoe UI Semibold', 14), background='#1c6659', padding=16)
        style.configure('TEntry', fieldbackground='#21313e', foreground='#ffffff', insertcolor='white')
        style.configure('TCombobox', fieldbackground='#21313e', foreground='#ffffff', arrowcolor='white')
        style.map('TCombobox', fieldbackground=[('readonly', '#21313e')], foreground=[('readonly', '#ffffff')])
        style.configure('TCheckbutton', background='#101820', foreground='#e7eef4')
        body = ttk.Frame(root, padding=24)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Omni Local', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='A voice conversation, entirely on your machine.', foreground='#a3b8c7').pack(anchor='w', pady=(0, 16))
        server = ttk.Frame(body)
        server.pack(fill='x')
        ttk.Label(server, text='Server').pack(side='left', padx=(0, 8))
        self.url_entry = ttk.Entry(server, textvariable=self.url, width=29)
        self.url_entry.pack(side='left', fill='x', expand=True)
        ttk.Label(server, text='Router model').pack(side='left', padx=8)
        self.model_entry = ttk.Entry(server, textvariable=self.model, width=13)
        self.model_entry.pack(side='left')
        self.connect_button = ttk.Button(server, text='Connect', command=self.toggle_connection)
        self.connect_button.pack(side='left', padx=(8, 0))
        ttk.Button(server, text='Health', command=self.check_health).pack(side='left', padx=(8, 0))
        setup = ttk.Frame(body)
        setup.pack(fill='x', pady=(7,0))
        self.start_button = ttk.Button(setup, text='Start local voice server', command=self.start_local)
        self.start_button.pack(side='left')
        self.find_button = ttk.Button(setup, text='Find running servers', command=self.find_servers)
        self.find_button.pack(side='left', padx=8)
        devices = ttk.Frame(body)
        devices.pack(fill='x', pady=12)
        self.input_box = ttk.Combobox(devices, state='readonly', width=34)
        self.output_box = ttk.Combobox(devices, state='readonly', width=34)
        for label, box in [('Microphone', self.input_box), ('Speakers', self.output_box)]:
            ttk.Label(devices, text=label).pack(side='left', padx=(0, 6))
            box.pack(side='left', fill='x', expand=True, padx=(0, 12))
        self.refresh_button = ttk.Button(devices, text='↻', width=3, command=self.refresh_devices)
        self.refresh_button.pack(side='left')
        settings = ttk.Frame(body)
        settings.pack(fill='x')
        self.mode_box = ttk.Combobox(settings, textvariable=self.mode, state='readonly', width=29,
                                    values=['Push to talk', 'Automatic • local energy VAD'])
        self.mode_box.pack(side='left')
        ttk.Label(settings, text='VAD level').pack(side='left', padx=(12, 5))
        self.threshold_box = ttk.Spinbox(settings, from_=100, to=5000, increment=50, textvariable=self.threshold, width=6)
        self.threshold_box.pack(side='left')
        ttk.Label(settings, text='Buffer ms').pack(side='left', padx=(12, 5))
        self.buffer_box = ttk.Spinbox(settings, from_=20, to=1000, increment=20, textvariable=self.buffer, width=6)
        self.buffer_box.pack(side='left')
        ttk.Label(body, text='Automatic mode uses local energy detection (700 ms silence); server VAD is unavailable.\n'
                  'Use headphones. Microphone waits during generation/playback; interruption is manual.',
                  foreground='#a3b8c7').pack(anchor='w', pady=(8, 12))
        self.talk = ttk.Button(body, text='Hold to talk  •  Space', style='Talk.TButton', state='disabled')
        self.talk.pack(fill='x')
        self.talk.bind('<ButtonPress-1>', self.begin)
        self.talk.bind('<ButtonRelease-1>', self.end)
        root.bind('<KeyPress-space>', self.key_begin)
        root.bind('<KeyRelease-space>', self.key_end)
        root.bind('<FocusOut>', self.focus_out)
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=10)
        self.pause_button = ttk.Checkbutton(actions, text='Pause microphone', variable=self.paused, command=lambda: self.client.command('pause', self.paused.get()))
        self.pause_button.pack(side='left')
        self.mute_button = ttk.Checkbutton(actions, text='Mute playback', variable=self.muted, command=lambda: self.client.command('mute', self.muted.get()))
        self.mute_button.pack(side='left', padx=18)
        self.cancel_button = ttk.Button(actions, text='Stop response / clear playback', command=lambda: self.client.command('cancel'))
        self.cancel_button.pack(side='right')
        self.level = ttk.Progressbar(body, maximum=100)
        self.level.pack(fill='x', pady=(0, 8))
        ttk.Label(body, textvariable=self.status, wraplength=860, foreground='#71e4c2').pack(anchor='w')
        ttk.Label(body, textvariable=self.details, wraplength=860, foreground='#a3b8c7').pack(anchor='w', pady=(3, 6))
        ttk.Label(body, textvariable=self.metrics, foreground='#a3b8c7').pack(anchor='w')
        transcript_frame = ttk.Frame(body)
        transcript_frame.pack(fill='both', expand=True, pady=(12, 0))
        self.log = tk.Text(transcript_frame, bg='#182630', fg='#e7eef4', font=('Segoe UI', 11),
                           relief='flat', wrap='word', padx=14, pady=12, state='disabled', height=9)
        scroll = ttk.Scrollbar(transcript_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log.pack(fill='both', expand=True)
        self.write('Welcome. Start your local audio-capable llama-server, then connect.\n'
                   'Conversation transcripts depend on the model. Nothing is saved to disk.\n')
        self.refresh_devices()
        self.set_controls(False)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(40, self.poll)

    def write(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        self.log.see('end')
        self.log.configure(state='disabled')

    def refresh_devices(self):
        try:
            devices = sd.query_devices()
            self.inputs = [None] + [i for i, d in enumerate(devices) if d['max_input_channels'] > 0]
            self.outputs = [None] + [i for i, d in enumerate(devices) if d['max_output_channels'] > 0]
            for box, ids in [(self.input_box, self.inputs), (self.output_box, self.outputs)]:
                box['values'] = ['Windows default'] + [f'{i} · {devices[i]["name"]}' for i in ids[1:]]
                box.current(0)
            for box, ids, key in [(self.input_box, self.inputs, 'input_name'), (self.output_box, self.outputs, 'output_name')]:
                preferred = self.settings.get(key)
                if preferred:
                    for position, device_id in enumerate(ids[1:], 1):
                        if preferred.casefold() in devices[device_id]['name'].casefold():
                            box.current(position)
                            break
        except Exception as e:
            self.inputs = self.outputs = [None]
            self.status.set('Audio device discovery failed: ' + str(e))

    def set_controls(self, connected):
        self.connected = connected
        for box in (self.input_box, self.output_box, self.mode_box):
            box.configure(state='disabled' if connected else 'readonly')
        for widget in (self.url_entry, self.model_entry, self.threshold_box, self.buffer_box, self.refresh_button, self.start_button, self.find_button):
            widget.configure(state='disabled' if connected else 'normal')
        for widget in (self.pause_button, self.mute_button, self.cancel_button):
            widget.configure(state='normal' if connected else 'disabled')
        self.talk.configure(state='normal' if connected and self.mode.get() == 'Push to talk' else 'disabled')
        self.connect_button.configure(text='Disconnect' if connected else 'Connect')

    def toggle_connection(self):
        if self.connected:
            self.client.disconnect()
            self.status.set('Disconnecting…')
            return
        try:
            threshold, buffer_ms = self.threshold.get(), self.buffer.get()
            if not 100 <= threshold <= 5000 or not 20 <= buffer_ms <= 1000:
                raise ValueError('VAD level must be 100–5000; buffer must be 20–1000 ms.')
            self.paused.set(False)
            self.muted.set(False)
            self.latency = None
            self.client.start(self.url.get(), self.inputs[max(0, self.input_box.current())],
                              self.outputs[max(0, self.output_box.current())], self.mode.get(),
                              threshold, buffer_ms, self.model.get().strip() or 'default')
            self.set_controls(True)
            self.talk.configure(state='disabled')
        except Exception as e:
            messagebox.showerror('Cannot connect', str(e))

    def begin(self, event=None):
        if self.connected and not self.pressed and self.mode.get() == 'Push to talk' and not self.paused.get():
            self.pressed = True
            self.client.command('begin')
            self.talk.configure(text='Listening… release to send')

    def end(self, event=None):
        if self.pressed:
            self.pressed = False
            self.client.command('end')
            self.talk.configure(text='Hold to talk  •  Space')

    def key_begin(self, event):
        if isinstance(self.root.focus_get(), (ttk.Entry, ttk.Spinbox, ttk.Combobox)):
            return
        self.begin()
        return 'break'

    def key_end(self, event):
        self.end()

    def focus_out(self, event):
        # A release outside the app must still end the turn.
        self.root.after(30, lambda: self.end() if self.root.focus_displayof() is None else None)

    def check_health(self):
        url = self.url.get()
        def check():
            try:
                status, body = health(url)
                self.events.put(('notice', f'Health HTTP {status}: {body}. Connect verifies native realtime audio capability.'))
            except Exception as e:
                self.events.put(('notice', 'Health check failed: ' + str(e)))
        threading.Thread(target=check, daemon=True).start()

    def start_local(self):
        self.start_button.configure(state='disabled')
        def start():
            try:
                url, model = start_server(self.settings, lambda k,v:self.events.put((k,v)))
                self.events.put(('server_started', (url, model)))
            except Exception as e:
                self.events.put(('setup_error', str(e)))
        threading.Thread(target=start, daemon=True).start()

    def find_servers(self):
        self.find_button.configure(state='disabled')
        self.status.set('Checking local ports 8081, 9931, 8080 and 1234…')
        def scan():
            self.events.put(('servers', discover()))
        threading.Thread(target=scan, daemon=True).start()

    def poll(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'status':
                self.status.set(value)
            elif kind == 'ready':
                self.settings.update(url=self.url.get(), model=self.model.get(), buffer_ms=self.buffer.get())
                try:
                    save_settings(self.settings)
                except OSError as e:
                    self.write('Could not save settings: '+str(e)+'\n')
                self.details.set(value)
                self.status.set('Ready • hold to talk' if self.mode.get() == 'Push to talk' else 'Listening • local energy VAD')
                self.talk.configure(state='normal' if self.mode.get() == 'Push to talk' else 'disabled')
                self.write('\nConnected • ' + value + '\n')
            elif kind == 'turn':
                self.assistant_open = False
                self.write('\n' + value + '\n')
            elif kind == 'text':
                if not self.assistant_open:
                    self.write('Omni • ')
                    self.assistant_open = True
                self.write(value)
            elif kind == 'transcript':
                item = value.get('item_id', '?')
                if value['type'].endswith('.delta'):
                    self.transcripts[item] = self.transcripts.get(item, '') + value.get('delta', '')
                else:
                    text = value.get('transcript') or self.transcripts.get(item, '')
                    self.write('\nYou • ' + text + '\n')
                    self.transcripts.pop(item, None)
            elif kind == 'done':
                self.write('\n[' + value + ']\n')
                self.assistant_open = False
            elif kind == 'latency':
                self.latency = value
            elif kind == 'level':
                self.level['value'] = value
            elif kind == 'stats':
                pending, underruns, dropped, capture, responding = value
                latency = f'{self.latency:.0f} ms' if self.latency is not None else '—'
                self.metrics.set(f'First audio: {latency}   |   Buffered: {pending*1000:.0f} ms   |   Underruns: {underruns}   |   Input warnings: {dropped}')
                if not capture:
                    self.level['value'] = 0
            elif kind in ('notice', 'error'):
                self.write('\n' + ('ERROR • ' if kind == 'error' else '') + value + '\n')
                self.status.set('Connection failed • see details below' if kind == 'error' else value)
            elif kind == 'disconnected':
                self.set_controls(False)
                self.end()
                self.details.set('Disconnected • no microphone audio is being sent')
                self.write('\nDisconnected.\n')
            elif kind == 'server_started':
                self.start_button.configure(state='normal')
                self.url.set(value[0])
                self.model.set(value[1])
                self.status.set('Local native voice server ready • connecting…')
                if not self.connected:
                    self.toggle_connection()
            elif kind == 'setup_error':
                self.start_button.configure(state='normal')
                self.status.set('Local server setup failed • see details below')
                self.write('\nSETUP • '+value+'\n')
            elif kind == 'servers':
                self.find_button.configure(state='normal')
                for server in value:
                    self.write(f"\n{server['url']} • {server['model']} • {server['description']}\n")
                if value:
                    self.url.set(value[0]['url'])
                    self.model.set(value[0]['model'])
                    self.status.set('Native audio server found • click Connect' if value[0]['native_audio'] else
                                    'Server found, but audio is unavailable. Start the configured local voice server.')
                else:
                    self.status.set('No running server found. Use Start local voice server.')
        self.root.after(40, self.poll)

    def close(self):
        self.client.disconnect()
        self.root.destroy()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test', action='store_true', help='Open GUI briefly, enumerate audio, then exit')
    parser.add_argument('--start-server', action='store_true', help='Start configured local-file server and connect')
    parser.add_argument('--connect', action='store_true', help='Connect to saved local server')
    args = parser.parse_args()
    root = create_root()
    app = App(root)
    if args.smoke_test:
        root.after(1500, app.close)
    elif args.start_server:
        root.after(200, app.start_local)
    elif args.connect:
        root.after(200, app.toggle_connection)
    root.mainloop()

if __name__ == '__main__':
    main()
