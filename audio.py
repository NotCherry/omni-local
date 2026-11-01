"""Bounded PCM playback and simple local energy VAD; no ASR or TTS."""
import audioop
import collections
import threading

class Playback:
    def __init__(self, rate, prebuffer_ms=100):
        self.rate = rate
        self.prebuffer = rate * 2 * prebuffer_ms // 1000
        self.data = bytearray()
        self.lock = threading.Lock()
        self.playing = False
        self.finished = False
        self.muted = False
        self.underruns = 0
        self.overflows = 0

    def add(self, pcm):
        with self.lock:
            if self.muted:
                return
            if len(self.data) + len(pcm) > self.rate * 2 * 30:
                self.overflows += 1
                raise RuntimeError('Playback backlog exceeded 30 s. Response stopped; reduce generation load.')
            self.data.extend(pcm)

    def clear(self):
        with self.lock:
            self.data.clear()
            self.playing = self.finished = False

    def pending(self):
        with self.lock:
            return len(self.data) / (2 * self.rate)

    def callback(self, outdata, frames, timing, status):
        n = frames * 2
        with self.lock:
            if status:
                self.underruns += 1
            if not self.playing and (len(self.data) >= self.prebuffer or self.finished):
                self.playing = bool(self.data)
            if self.playing and not self.muted:
                count = min(n, len(self.data))
                outdata[:count] = self.data[:count]
                del self.data[:count]
                outdata[count:] = b'\0' * (n - count)
                if count < n:
                    if not self.finished:
                        self.underruns += 1
                    self.playing = False
            else:
                outdata[:] = b'\0' * n

class EnergyVAD:
    """20 ms frames; 200 ms pre-roll, 60 ms onset, 700 ms silence, 20 s cap."""
    def __init__(self, threshold=450):
        self.threshold = threshold
        self.reset()

    def reset(self):
        self.preroll = collections.deque(maxlen=10)
        self.active = False
        self.onset = self.silence = self.frames = 0

    def feed(self, pcm):
        loud = audioop.rms(pcm, 2) >= self.threshold
        if not self.active:
            self.preroll.append(pcm)
            self.onset = self.onset + 1 if loud else 0
            if self.onset < 3:
                return b'', False
            self.active = True
            self.frames = len(self.preroll)
            return b''.join(self.preroll), False
        self.frames += 1
        self.silence = 0 if loud else self.silence + 1
        ended = self.silence >= 35 or self.frames >= 1000
        if ended:
            self.reset()
        return pcm, ended
