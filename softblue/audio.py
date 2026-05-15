"""Audio output: sounddevice primary, aplay/paplay fallbacks."""

from __future__ import annotations

import shutil
import subprocess
import wave

import numpy as np


class NoAudioBackendError(RuntimeError):
    pass


class AudioOutput:
    """Unified audio output with auto-detected backend."""

    def __init__(self) -> None:
        self.backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> str:
        try:
            import sounddevice  # noqa: F401

            return "sounddevice"
        except Exception:
            pass
        if shutil.which("paplay"):
            return "paplay"
        if shutil.which("aplay"):
            return "aplay"
        return "none"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def play(self, samples: np.ndarray, sample_rate: int = 8000, device=None) -> None:
        if self.backend == "sounddevice":
            import sounddevice as sd

            sd.play(samples, samplerate=sample_rate, device=device)
            sd.wait()
        elif self.backend in ("aplay", "paplay"):
            self._play_pipe(samples, sample_rate, device)
        else:
            raise NoAudioBackendError(
                "No audio output available — install the [audio] extra "
                "or use WAV export instead."
            )

    def _play_pipe(self, samples: np.ndarray, sample_rate: int, device) -> None:
        """Pipe a WAV stream to aplay/paplay via stdin."""
        from .engine import ToneEngine

        wav = ToneEngine().to_wav_bytes(samples, sample_rate)
        if self.backend == "aplay":
            cmd = ["aplay", "-q"]
            if device:
                cmd += ["-D", str(device)]
            cmd += ["-"]
        else:  # paplay
            cmd = ["paplay"]
            if device:
                cmd += [f"--device={device}"]
            cmd += ["/dev/stdin"]
        proc = subprocess.run(cmd, input=wav, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.backend} failed: {proc.stderr.decode(errors='replace')}"
            )

    def get_devices(self) -> list[dict]:
        if self.backend == "sounddevice":
            import sounddevice as sd

            return [
                {
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                }
                for i, d in enumerate(sd.query_devices())
                if d["max_output_channels"] > 0
            ]
        if self.backend in ("aplay", "paplay"):
            return [{"index": 0, "name": f"system default ({self.backend})", "channels": 1}]
        return []
