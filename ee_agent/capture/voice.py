"""Voice in, voice out, interruptible -- and entirely optional (abilities 1-5).

Voice is a shell, never a dependency. With ``voice.enabled: false`` the agent
loses no capability whatsoever: every path in this codebase is reachable from
text, and nothing imports this module at import time.

Local and free by default: Whisper for input, the OS's own speech synthesis for
output. Cloud voice is offered only if the client asks for it, and the cost
notifier prices it first.
"""
from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator


@dataclass
class VoiceStatus:
    input_available: bool
    output_available: bool
    input_engine: str = "none"
    output_engine: str = "none"
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"[voice] input: {self.input_engine} ({'ready' if self.input_available else 'unavailable'}), "
            f"output: {self.output_engine} ({'ready' if self.output_available else 'unavailable'})"
        ]
        lines += [f"        {n}" for n in self.notes]
        if not (self.input_available and self.output_available):
            lines.append(
                "        Voice is a shell. Everything works without it -- nothing is gated behind a microphone."
            )
        return "\n".join(lines)


def status() -> VoiceStatus:
    notes: list[str] = []
    try:
        import faster_whisper  # noqa: F401

        input_engine, input_ok = "faster-whisper (local, free)", True
    except ImportError:
        input_engine, input_ok = "none", False
        notes.append("install `faster-whisper` and `sounddevice` for local push-to-talk speech input")

    output_engine, output_ok = _tts_backend()
    if not output_ok:
        notes.append("install `pyttsx3` for local speech output, or use the OS voice on macOS/Windows")
    return VoiceStatus(input_ok, output_ok, input_engine, output_engine, notes)


def _tts_backend() -> tuple[str, bool]:
    if sys.platform == "darwin" and shutil.which("say"):
        return "macOS say (local, free)", True
    if sys.platform == "win32":
        return "Windows SAPI (local, free)", True
    try:
        import pyttsx3  # noqa: F401

        return "pyttsx3 (local, free)", True
    except ImportError:
        pass
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak (local, free)", True
    return "none", False


class Speaker:
    """Text to speech, interruptible mid-sentence (ability 3).

    Speech is queued sentence by sentence in a worker thread; :meth:`interrupt`
    drops the queue and stops the current utterance, which is what makes talking
    over the agent work.
    """

    def __init__(self, enabled: bool = True, rate: int = 180, voice: str | None = None):
        self.enabled = enabled and status().output_available
        self.rate = rate
        self.voice = voice
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self.spoken: list[str] = []  # transcript, always kept even when muted

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def say(self, text: str) -> None:
        self.spoken.append(text)
        if not self.enabled:
            return
        self.start()
        for sentence in _sentences(text):
            self._queue.put(sentence)

    def interrupt(self) -> None:
        """The client cut in. Stop talking immediately."""
        self._stop.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._stop.clear()

    def close(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:  # pragma: no cover - audio device
        while True:
            item = self._queue.get()
            if item is None:
                return
            if self._stop.is_set():
                continue
            try:
                self._speak_once(item)
            except Exception:
                continue

    def _speak_once(self, text: str) -> None:  # pragma: no cover - audio device
        if sys.platform == "darwin" and shutil.which("say"):
            args = ["say", "-r", str(self.rate)]
            if self.voice and self.voice != "default":
                args += ["-v", self.voice]
            self._process = subprocess.Popen([*args, text])
            self._process.wait()
            return
        if sys.platform == "win32":
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Rate = {max(-10, min(10, (self.rate - 180) // 20))}; "
                f"$s.Speak([Console]::In.ReadToEnd())"
            )
            self._process = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", script], stdin=subprocess.PIPE, text=True
            )
            self._process.communicate(text)
            return
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate)
            engine.say(text)
            engine.runAndWait()
        except Exception:
            if shutil.which("espeak-ng") or shutil.which("espeak"):
                binary = shutil.which("espeak-ng") or shutil.which("espeak")
                self._process = subprocess.Popen([binary, text])
                self._process.wait()


class Listener:
    """Push-to-talk speech input via local Whisper. Free, offline, no key."""

    def __init__(self, model: str = "base.en", sample_rate: int = 16000):
        self.model_name = model
        self.sample_rate = sample_rate
        self._model = None

    @property
    def available(self) -> bool:
        return status().input_available

    def _load(self):  # pragma: no cover - heavy optional dependency
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def transcribe_file(self, path: str) -> str:  # pragma: no cover - optional
        segments, _info = self._load().transcribe(path, beam_size=1)
        return " ".join(segment.text.strip() for segment in segments).strip()

    def listen(self, seconds: float = 30.0, stop_event: threading.Event | None = None) -> str:  # pragma: no cover
        """Record until the key is released (or ``seconds`` elapse), then transcribe."""
        import numpy as np
        import sounddevice as sd
        import tempfile
        import wave

        frames: list[bytes] = []

        def callback(indata, _frames, _time, _status):
            frames.append(bytes(indata))

        with sd.RawInputStream(samplerate=self.sample_rate, channels=1, dtype="int16", callback=callback):
            waited = 0.0
            while waited < seconds and not (stop_event and stop_event.is_set()):
                sd.sleep(100)
                waited += 0.1

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            with wave.open(fh.name, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(b"".join(frames))
            return self.transcribe_file(fh.name)


class VoiceShell:
    """Ties the two together with a text fallback that is always available."""

    def __init__(self, enabled: bool = False, rate: int = 180, input_fn: Callable[[str], str] = input):
        self.enabled = enabled
        self.speaker = Speaker(enabled=enabled, rate=rate)
        self.listener = Listener() if enabled else None
        self.input_fn = input_fn

    def say(self, text: str, also_print: bool = True) -> None:
        if also_print:
            print(text)
        self.speaker.say(text)

    def ask(self, prompt: str) -> str:
        """Speak the question, take the answer by voice if available, else text."""
        self.say(prompt)
        if self.enabled and self.listener and self.listener.available:  # pragma: no cover
            try:
                return self.listener.listen()
            except Exception:
                pass
        return self.input_fn("> ")

    def interrupt(self) -> None:
        self.speaker.interrupt()


def _sentences(text: str) -> Iterator[str]:
    import re

    for part in re.split(r"(?<=[.!?])\s+", text.strip()):
        if part.strip():
            yield part.strip()
