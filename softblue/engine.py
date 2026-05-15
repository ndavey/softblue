"""Core MF tone synthesis engine (pure numpy + stdlib)."""

from __future__ import annotations

import io
import wave
from typing import Iterator

import numpy as np

from .config import Config

FADE_SECONDS = 0.005  # 5ms raised-cosine edges to suppress clicks


class InvalidDigitError(ValueError):
    """Raised when a sequence contains a character that is not a valid MF digit."""

    def __init__(self, digit: str):
        self.digit = digit
        super().__init__(f'"{digit}" is not a valid MF digit (valid: 0-9)')


class ToneEngine:
    """Bell System R1 MF tone synthesis."""

    MF_DIGITS = {
        "1": (700, 900), "2": (700, 1100), "3": (900, 1100),
        "4": (700, 1300), "5": (900, 1300), "6": (1100, 1300),
        "7": (700, 1500), "8": (900, 1500), "9": (1100, 1500),
        "0": (1300, 1500),
    }
    MF_SPECIAL = {
        "KP": (1100, 1700),
        "ST": (1500, 1700),
        "ST2": (900, 1700),
        "ST3": (1300, 1700),
    }
    SEIZURE_FREQ = 2600

    # ---- low-level synthesis -------------------------------------------------

    def generate_tone(
        self,
        frequencies,
        duration: float,
        sample_rate: int = 8000,
        amplitude: float = 0.7,
    ) -> np.ndarray:
        num_samples = int(sample_rate * duration)
        if num_samples <= 0:
            return np.zeros(0, dtype=np.float32)
        t = np.linspace(0, duration, num_samples, endpoint=False)
        samples = sum(np.sin(2 * np.pi * f * t) for f in frequencies)
        samples = samples / len(frequencies) * amplitude

        fade = int(sample_rate * FADE_SECONDS)
        if fade > 0 and num_samples > fade * 2:
            samples[:fade] *= np.linspace(0, 1, fade)
            samples[-fade:] *= np.linspace(1, 0, fade)
        return samples.astype(np.float32)

    def generate_silence(self, duration: float, sample_rate: int = 8000) -> np.ndarray:
        return np.zeros(max(0, int(sample_rate * duration)), dtype=np.float32)

    # ---- sequence ------------------------------------------------------------

    @classmethod
    def validate_digits(cls, digits: str) -> str:
        """Normalise/validate a digit string, raising on the first bad char."""
        cleaned = (digits or "").strip()
        for ch in cleaned:
            if ch in (" ", "-"):
                continue
            if ch not in cls.MF_DIGITS:
                raise InvalidDigitError(ch)
        return cleaned

    def build_sequence(self, digits: str, config: Config) -> np.ndarray:
        """Build a complete MF sequence: seize [→ wink → KP → digits → ST]."""
        sr = config.sample_rate
        amp = config.amplitude
        parts: list[np.ndarray] = [
            self.generate_tone([self.SEIZURE_FREQ], config.seize_duration, sr, amp)
        ]

        if not config.seize_only:
            digits = self.validate_digits(digits)
            parts.append(self.generate_silence(config.wink_delay, sr))
            parts.append(
                self.generate_tone(self.MF_SPECIAL["KP"], config.kp_duration, sr, amp)
            )
            real = [d for d in digits if d not in (" ", "-")]
            for i, digit in enumerate(real):
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
                parts.append(
                    self.generate_tone(self.MF_DIGITS[digit], config.digit_duration, sr, amp)
                )
            parts.append(self.generate_silence(config.inter_digit_gap, sr))
            parts.append(
                self.generate_tone(self.MF_SPECIAL["ST"], config.st_duration, sr, amp)
            )

        out = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return self._normalize(out)

    def generate_chunks(
        self, digits: str, config: Config, chunk_ms: int = 100
    ) -> Iterator[bytes]:
        """Yield the sequence as int16 little-endian PCM chunks (for streaming)."""
        pcm = self.to_int16(self.build_sequence(digits, config))
        step = max(1, int(config.sample_rate * chunk_ms / 1000))
        for i in range(0, len(pcm), step):
            yield pcm[i : i + step].tobytes()

    # ---- conversion ----------------------------------------------------------

    @staticmethod
    def _normalize(samples: np.ndarray) -> np.ndarray:
        """Scale down only if the signal would clip (>1.0 full scale)."""
        if samples.size == 0:
            return samples
        peak = float(np.max(np.abs(samples)))
        if peak > 1.0:
            samples = samples / peak
        return samples.astype(np.float32)

    @staticmethod
    def to_int16(samples: np.ndarray) -> np.ndarray:
        clipped = np.clip(samples, -1.0, 1.0)
        return (clipped * 32767.0).astype(np.int16)

    def to_wav_bytes(self, samples: np.ndarray, sample_rate: int = 8000) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(self.to_int16(samples).tobytes())
        return buf.getvalue()

    def write_wav(self, path: str, samples: np.ndarray, sample_rate: int = 8000) -> None:
        with open(path, "wb") as f:
            f.write(self.to_wav_bytes(samples, sample_rate))

    @staticmethod
    def read_wav(path: str) -> tuple[np.ndarray, int]:
        with wave.open(path, "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            raw = w.readframes(n)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return data, sr
