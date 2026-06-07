"""Core tone synthesis engine (pure numpy + stdlib).

Modes:
- ``mf_r1`` — Bell System R1 MF (2-of-6, 2600 Hz seize). Default; preserves
  the original auto-wrap behavior (seize → wink → KP → digits → ST) when the
  digit string contains only 0-9. If the string contains any of the inline
  control characters ``k`` (KP), ``s`` (ST), ``z`` (seize), ``x`` (clear),
  or ``.`` (idle), the sequence is emitted literally with no auto-wrap.
- ``c5`` — CCITT #5 international. Same 2-of-6 MF digits as R1 but with a
  2400 Hz signaling frequency for seize/clear.
- ``dtmf`` — standard 16-key touch-tone (0-9, *, #, A-D). No seize/KP/ST.
- ``us_redbox`` — US payphone coin tones. ``1``=nickel, ``2``=dime,
  ``3``=quarter, ``4``=dollar. Two schemes selectable via ``Config.coin_scheme``:
  ``acts`` (real Bell 1700+2200 Hz dual tone) and ``phreakme`` (single 1700 Hz,
  what the emulated PhreakMe payphone listens for).
- ``uk_redbox`` — UK trunk pips. ``1``=10p (200ms 1000Hz), ``2``=50p (350ms).
- ``pulse_2600`` — dial-pulse / "whistle" method. Each digit emits N pulses
  of 2600 Hz (0 = 10 pulses); 60ms break / 40ms make per pulse.
- ``bell_3slot`` — 3-slot payphone gong/bell tones (Western Electric coin
  signal, sounded as the caller deposits). ``1``=nickel (one 1664 Hz ding),
  ``2``=dime (two 1664 Hz dings), ``3``=quarter (one 800 Hz gong). Uses an
  exponential bell-decay envelope rather than the flat MF/red-box tone.
- ``green_box`` — operator/TSPS coin-control signals sent by the *called*
  party over the voice path of a connected fortress (payphone) call.
  ``c``=coin collect (700+1100 Hz), ``r``=coin return (1100+1700 Hz),
  ``b``=ringback (700+1700 Hz). Each control tone is preceded by an operator
  release "wink", selectable via ``Config.green_wink``: ``2600`` (a 2600 Hz
  90ms / 60ms-silence / 900ms operator-release signal) or ``mf8`` (an MF "8"
  900+1500 Hz, 90ms wink + 60ms silence).
"""

from __future__ import annotations

import io
import wave
from typing import Iterator

import numpy as np

from .config import Config

FADE_SECONDS = 0.005  # 5ms raised-cosine edges to suppress clicks

MODES = ("mf_r1", "c5", "dtmf", "us_redbox", "uk_redbox", "pulse_2600",
         "bell_3slot", "green_box")
COIN_SCHEMES = ("acts", "phreakme")
GREEN_WINKS = ("2600", "mf8")


class InvalidDigitError(ValueError):
    """Raised when a sequence contains a character invalid for the current mode."""

    _MODE_LABEL = {
        "mf_r1": "MF", "c5": "C5", "dtmf": "DTMF",
        "us_redbox": "US red-box", "uk_redbox": "UK red-box",
        "pulse_2600": "2600-pulse", "bell_3slot": "3-slot bell",
        "green_box": "green-box",
    }

    def __init__(self, digit: str, mode: str = "mf_r1"):
        self.digit = digit
        self.mode = mode
        super().__init__(
            f'"{digit}" is not a valid {self._MODE_LABEL.get(mode, mode)} digit')


class InvalidModeError(ValueError):
    pass


class ToneEngine:
    """Multi-mode bluebox / DTMF / red-box tone synthesis."""

    # ---- tone tables ---------------------------------------------------------

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
    SEIZURE_FREQ = 2600   # MF/R1
    C5_SF_FREQ = 2400     # CCITT #5

    DTMF_DIGITS = {
        "1": (1209, 697), "2": (1336, 697), "3": (1477, 697), "A": (1633, 697),
        "4": (1209, 770), "5": (1336, 770), "6": (1477, 770), "B": (1633, 770),
        "7": (1209, 852), "8": (1336, 852), "9": (1477, 852), "C": (1633, 852),
        "*": (1209, 941), "0": (1336, 941), "#": (1477, 941), "D": (1633, 941),
    }

    # US Red Box coin specs. Each entry is a list of (on_seconds, off_seconds)
    # bursts; the carrier (single or dual freq) is set by ``coin_scheme``.
    US_REDBOX_BURSTS = {
        "1": [(0.066, 0.100)],                                       # nickel
        "2": [(0.066, 0.066), (0.066, 0.100)],                       # dime
        "3": [(0.033, 0.033)] * 4 + [(0.033, 0.100)],                # quarter
        "4": [(0.650, 0.100)],                                       # dollar
    }
    US_REDBOX_FREQS_ACTS = (1700, 2200)
    US_REDBOX_FREQS_PHREAKME = (1700,)

    UK_REDBOX = {
        "1": (1000, 0.200),  # 10p
        "2": (1000, 0.350),  # 50p
    }

    PULSE_2600_BREAK_S = 0.060
    PULSE_2600_MAKE_S = 0.040

    # 3-slot payphone gong/bell tones — ``(freq, pulses, bell_seconds, gap_seconds)``.
    BELL_3SLOT = {
        "1": (1664, 1, 0.35, 0.0),   # nickel — one ding
        "2": (1664, 2, 0.35, 0.20),  # dime — two dings
        "3": (800, 1, 0.70, 0.0),    # quarter — gong
    }
    BELL_FLOOR = 0.0001  # exponential decay target (matches Web Audio bell)

    # Green box: operator/TSPS coin-control tones, each ``(freq_pair, on_seconds)``.
    GREEN_BOX = {
        "c": ((700, 1100), 1.0),    # coin collect
        "r": ((1100, 1700), 1.0),   # coin return
        "b": ((700, 1700), 2.0),    # ringback
    }
    # Operator-release "wink" preceding each green-box control tone.
    GREEN_WINK_FREQ = 2600          # 2600 Hz operator-release signal
    GREEN_WINK_MF8 = (900, 1500)    # MF "8" wink alternative
    GREEN_WINK_ON1_S = 0.090        # first burst (both wink styles)
    GREEN_WINK_GAP_S = 0.060        # inter-burst silence
    GREEN_WINK_ON2_S = 0.900        # second 2600 Hz burst (2600 style only)

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

    def generate_bell(
        self,
        freq: float,
        duration: float,
        sample_rate: int = 8000,
        amplitude: float = 0.7,
    ) -> np.ndarray:
        """A struck-bell tone: immediate attack, exponential decay to ~silence."""
        num_samples = int(sample_rate * duration)
        if num_samples <= 0 or amplitude <= 0:
            return np.zeros(max(0, num_samples), dtype=np.float32)
        t = np.linspace(0, duration, num_samples, endpoint=False)
        envelope = amplitude * (self.BELL_FLOOR / amplitude) ** (t / duration)
        samples = np.sin(2 * np.pi * freq * t) * envelope
        return samples.astype(np.float32)

    # ---- validation ----------------------------------------------------------

    @classmethod
    def validate_digits(cls, digits: str, mode: str = "mf_r1") -> str:
        """Normalise/validate a digit string for the given mode."""
        if mode not in MODES:
            raise InvalidModeError(f"unknown mode {mode!r} (valid: {', '.join(MODES)})")
        cleaned = (digits or "").strip()
        if mode in ("mf_r1", "c5", "green_box"):
            cleaned = cleaned.lower()
        for ch in cleaned:
            if ch in (" ", "-"):
                continue
            if not cls._is_valid_for_mode(ch, mode):
                raise InvalidDigitError(ch, mode)
        return cleaned

    @classmethod
    def _is_valid_for_mode(cls, ch: str, mode: str) -> bool:
        if mode in ("mf_r1", "c5"):
            return ch in cls.MF_DIGITS or ch in ("k", "s", "z", "x", ".")
        if mode == "dtmf":
            return ch.upper() in cls.DTMF_DIGITS
        if mode == "us_redbox":
            return ch in cls.US_REDBOX_BURSTS
        if mode == "uk_redbox":
            return ch in cls.UK_REDBOX
        if mode == "pulse_2600":
            return ch.isdigit()
        if mode == "bell_3slot":
            return ch in cls.BELL_3SLOT
        if mode == "green_box":
            return ch in cls.GREEN_BOX
        return False

    # ---- sequence dispatch ---------------------------------------------------

    def build_sequence(self, digits: str, config: Config) -> np.ndarray:
        """Build a complete tone sequence for ``config.mode``."""
        mode = getattr(config, "mode", "mf_r1") or "mf_r1"
        cleaned = self.validate_digits(digits, mode)
        if mode == "mf_r1":
            out = self._build_mf(cleaned, config, sf_freq=self.SEIZURE_FREQ)
        elif mode == "c5":
            out = self._build_mf(cleaned, config, sf_freq=self.C5_SF_FREQ)
        elif mode == "dtmf":
            out = self._build_dtmf(cleaned, config)
        elif mode == "us_redbox":
            out = self._build_us_redbox(cleaned, config)
        elif mode == "uk_redbox":
            out = self._build_uk_redbox(cleaned, config)
        elif mode == "pulse_2600":
            out = self._build_pulse_2600(cleaned, config)
        elif mode == "bell_3slot":
            out = self._build_bell_3slot(cleaned, config)
        elif mode == "green_box":
            out = self._build_green_box(cleaned, config)
        else:  # pragma: no cover - guarded by validate_digits
            raise InvalidModeError(mode)
        return self._normalize(out)

    # ---- mode: MF / C5 -------------------------------------------------------

    def _has_inline(self, digits: str) -> bool:
        return any(ch in ("k", "s", "z", "x", ".") for ch in digits)

    def _build_mf(self, digits: str, config: Config, sf_freq: int) -> np.ndarray:
        """MF R1 / C5. Auto-wrap (seize→wink→KP→digits→ST) unless inline
        control chars are present, in which case emit literally."""
        sr = config.sample_rate
        amp = config.amplitude
        parts: list[np.ndarray] = []

        if config.seize_only:
            parts.append(self.generate_tone([sf_freq], config.seize_duration, sr, amp))
            return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

        inline = self._has_inline(digits)

        if inline:
            # Literal mode — emit exactly what the user typed, with inter-digit
            # gaps between events. No auto seize/KP/ST.
            first = True
            for ch in digits:
                if ch in (" ", "-"):
                    continue
                if not first:
                    parts.append(self.generate_silence(config.inter_digit_gap, sr))
                first = False
                if ch in self.MF_DIGITS:
                    parts.append(self.generate_tone(
                        self.MF_DIGITS[ch], config.digit_duration, sr, amp))
                elif ch == "k":
                    parts.append(self.generate_tone(
                        self.MF_SPECIAL["KP"], config.kp_duration, sr, amp))
                elif ch == "s":
                    parts.append(self.generate_tone(
                        self.MF_SPECIAL["ST"], config.st_duration, sr, amp))
                elif ch == "z":
                    parts.append(self.generate_tone(
                        [sf_freq], config.seize_duration, sr, amp))
                elif ch == "x":
                    parts.append(self.generate_tone([sf_freq], 0.100, sr, amp))
                elif ch == ".":
                    parts.append(self.generate_tone(
                        [sf_freq], config.digit_duration, sr, amp))
        else:
            # Backward-compatible auto-wrap path.
            parts.append(self.generate_tone([sf_freq], config.seize_duration, sr, amp))
            parts.append(self.generate_silence(config.wink_delay, sr))
            parts.append(self.generate_tone(
                self.MF_SPECIAL["KP"], config.kp_duration, sr, amp))
            for ch in digits:
                if ch in (" ", "-"):
                    continue
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
                parts.append(self.generate_tone(
                    self.MF_DIGITS[ch], config.digit_duration, sr, amp))
            parts.append(self.generate_silence(config.inter_digit_gap, sr))
            parts.append(self.generate_tone(
                self.MF_SPECIAL["ST"], config.st_duration, sr, amp))

        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- mode: DTMF ----------------------------------------------------------

    def _build_dtmf(self, digits: str, config: Config) -> np.ndarray:
        sr, amp = config.sample_rate, config.amplitude
        parts: list[np.ndarray] = []
        first = True
        for ch in digits:
            if ch in (" ", "-"):
                continue
            if not first:
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
            first = False
            parts.append(self.generate_tone(
                self.DTMF_DIGITS[ch.upper()], config.digit_duration, sr, amp))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- mode: US Red Box ----------------------------------------------------

    def _build_us_redbox(self, digits: str, config: Config) -> np.ndarray:
        scheme = getattr(config, "coin_scheme", "acts") or "acts"
        if scheme not in COIN_SCHEMES:
            raise InvalidModeError(
                f"unknown coin_scheme {scheme!r} (valid: {', '.join(COIN_SCHEMES)})")
        freqs = (self.US_REDBOX_FREQS_ACTS if scheme == "acts"
                 else self.US_REDBOX_FREQS_PHREAKME)
        sr, amp = config.sample_rate, config.amplitude
        parts: list[np.ndarray] = []
        first = True
        for ch in digits:
            if ch in (" ", "-"):
                continue
            if not first:
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
            first = False
            for on_s, off_s in self.US_REDBOX_BURSTS[ch]:
                parts.append(self.generate_tone(list(freqs), on_s, sr, amp))
                if off_s > 0:
                    parts.append(self.generate_silence(off_s, sr))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- mode: UK Red Box ----------------------------------------------------

    def _build_uk_redbox(self, digits: str, config: Config) -> np.ndarray:
        sr, amp = config.sample_rate, config.amplitude
        parts: list[np.ndarray] = []
        first = True
        for ch in digits:
            if ch in (" ", "-"):
                continue
            if not first:
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
            first = False
            freq, dur = self.UK_REDBOX[ch]
            parts.append(self.generate_tone([freq], dur, sr, amp))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- mode: 2600 dial pulse ----------------------------------------------

    def _build_pulse_2600(self, digits: str, config: Config) -> np.ndarray:
        sr, amp = config.sample_rate, config.amplitude
        parts: list[np.ndarray] = []
        first = True
        for ch in digits:
            if ch in (" ", "-"):
                continue
            if not first:
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
            first = False
            n = int(ch)
            pulses = 10 if n == 0 else n
            for _ in range(pulses):
                parts.append(self.generate_silence(self.PULSE_2600_BREAK_S, sr))
                parts.append(self.generate_tone(
                    [self.SEIZURE_FREQ], self.PULSE_2600_MAKE_S, sr, amp))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- mode: 3-slot bell ---------------------------------------------------

    def _build_bell_3slot(self, digits: str, config: Config) -> np.ndarray:
        """3-slot payphone gong/bell tones (exponential-decay envelope)."""
        sr, amp = config.sample_rate, config.amplitude
        parts: list[np.ndarray] = []
        first = True
        for ch in digits:
            if ch in (" ", "-"):
                continue
            if not first:
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
            first = False
            freq, pulses, bell_s, gap_s = self.BELL_3SLOT[ch]
            for i in range(pulses):
                if i:
                    parts.append(self.generate_silence(gap_s, sr))
                parts.append(self.generate_bell(freq, bell_s, sr, amp))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- mode: Green Box -----------------------------------------------------

    def _build_green_box(self, digits: str, config: Config) -> np.ndarray:
        """Operator/TSPS coin-control tones. Each symbol emits an operator
        release wink followed by its control tone (collect/return/ringback)."""
        wink = getattr(config, "green_wink", "2600") or "2600"
        if wink not in GREEN_WINKS:
            raise InvalidModeError(
                f"unknown green_wink {wink!r} (valid: {', '.join(GREEN_WINKS)})")
        sr, amp = config.sample_rate, config.amplitude
        parts: list[np.ndarray] = []
        first = True
        for ch in digits:
            if ch in (" ", "-"):
                continue
            if not first:
                parts.append(self.generate_silence(config.inter_digit_gap, sr))
            first = False
            # Operator release wink.
            if wink == "2600":
                parts.append(self.generate_tone(
                    [self.GREEN_WINK_FREQ], self.GREEN_WINK_ON1_S, sr, amp))
                parts.append(self.generate_silence(self.GREEN_WINK_GAP_S, sr))
                parts.append(self.generate_tone(
                    [self.GREEN_WINK_FREQ], self.GREEN_WINK_ON2_S, sr, amp))
            else:  # mf8
                parts.append(self.generate_tone(
                    list(self.GREEN_WINK_MF8), self.GREEN_WINK_ON1_S, sr, amp))
                parts.append(self.generate_silence(self.GREEN_WINK_GAP_S, sr))
            # Control tone.
            freqs, dur = self.GREEN_BOX[ch]
            parts.append(self.generate_tone(list(freqs), dur, sr, amp))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    # ---- macros --------------------------------------------------------------

    def build_macro(
        self,
        steps: list[dict],
        base_config: Config,
        preset_lookup=None,
    ) -> np.ndarray:
        """Render a macro (ordered list of steps) into a single sequence.

        Each step is either inline ``{mode, digits, config?, delay_after?}`` or
        a preset reference ``{preset, delay_after?}``. ``preset_lookup`` is a
        callable name → Preset used to resolve refs (the CLI/server pass in
        :py:meth:`softblue.presets.PresetManager.load`).
        """
        parts: list[np.ndarray] = []
        sr = base_config.sample_rate
        for i, step in enumerate(steps):
            if "preset" in step:
                if preset_lookup is None:
                    raise InvalidModeError(
                        f"step {i}: preset reference {step['preset']!r} "
                        "but no preset_lookup provided")
                p = preset_lookup(step["preset"])
                parts.append(self.build_sequence(p.digits, p.config))
            else:
                overrides = dict(step.get("config", {}) or {})
                # Step-level ``mode`` takes precedence over any in ``config``.
                if step.get("mode"):
                    overrides["mode"] = step["mode"]
                cfg = base_config.merged(**overrides)
                cfg.validate()
                parts.append(self.build_sequence(step.get("digits", ""), cfg))
            delay = step.get("delay_after", 0) or 0
            if delay > 0:
                parts.append(self.generate_silence(delay, sr))
        out = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return self._normalize(out)

    # ---- streaming -----------------------------------------------------------

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
