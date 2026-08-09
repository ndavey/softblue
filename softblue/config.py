"""Configuration: sequence parameters and app settings.

`Config` holds the parameters that define a generated MF sequence. It is the
single source of truth shared by the engine, CLI, presets and web layer, so
its field names are canonical (CLI long-options map 1:1 onto these names).

`Settings` holds app-wide preferences loaded from ~/.softblue/config.yaml.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


def config_dir() -> Path:
    """Base config dir, overridable via $SOFTBLUE_HOME (used by tests)."""
    return Path(os.environ.get("SOFTBLUE_HOME", Path.home() / ".softblue"))


def config_file() -> Path:
    return config_dir() / "config.yaml"


def preset_dir() -> Path:
    return config_dir() / "presets"


@dataclass
class Config:
    """Parameters that fully define an MF sequence."""

    seize_duration: float = 2.0
    wink_delay: float = 0.5
    digit_duration: float = 0.06
    inter_digit_gap: float = 0.1
    kp_duration: float = 0.1
    st_duration: float = 0.1
    amplitude: float = 0.7
    sample_rate: int = 8000
    # When true (mf_r1/c5 only), emit just the signaling tone — no wink/KP/digits/ST.
    seize_only: bool = False
    # Signaling mode — see engine.MODES.
    mode: str = "mf_r1"
    # US red-box coin scheme — see engine.US_REDBOX_FREQS (acts | nortel | phreakme).
    coin_scheme: str = "acts"
    # Red-box probe overrides. None = use the coin's standard value. These exist so
    # a black-box target can be swept over frequency/timing without code edits.
    coin_freqs: list[float] | None = None
    coin_on: float | None = None
    coin_gap: float | None = None
    # Discovered PhreakMe coin patterns, replacing engine.PHREAKME_COINS. Maps a
    # symbol to a list of ``[freqs | null, seconds, level_dbfs]`` segments — the
    # shape `softblue analyze` emits, so a changed scheme is playable at once.
    coin_spec: dict | None = None
    # MF KP/ST table: "standard" (Bell R1) or "coin" (PhreakMe payphone path,
    # KP 1700+2200 / ST 1500+2200). The web UI has always sent this; Config
    # silently dropped it, so a call carried different tones than the preview.
    mf_variant: str = "standard"
    # Override the 2600/2400 seize frequency (used by the UI's sweep lock).
    seize_freq: float | None = None
    # Green-box operator-release wink — "2600" (2600 Hz release signal) or "mf8" (MF "8").
    green_wink: str = "2600"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def merged(self, **overrides: Any) -> "Config":
        """Return a copy with non-None overrides applied."""
        data = self.to_dict()
        for k, v in overrides.items():
            if v is not None and k in data:
                data[k] = v
        return Config.from_dict(data)

    def validate(self) -> None:
        if not 0.0 <= self.amplitude <= 1.0:
            raise ValueError(f"amplitude must be 0.0–1.0, got {self.amplitude}")
        if self.sample_rate < 8000:
            # All tones (max 2600 Hz) must stay below Nyquist.
            raise ValueError(f"sample_rate must be >= 8000, got {self.sample_rate}")
        # Avoid circular import — engine imports Config.
        from .engine import COIN_SCHEMES, GREEN_WINKS, MF_VARIANTS, MODES

        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.coin_scheme not in COIN_SCHEMES:
            raise ValueError(
                f"coin_scheme must be one of {COIN_SCHEMES}, got {self.coin_scheme!r}")
        if self.mf_variant not in MF_VARIANTS:
            raise ValueError(
                f"mf_variant must be one of {MF_VARIANTS}, got {self.mf_variant!r}")
        if self.seize_freq is not None and not 0 < self.seize_freq < self.sample_rate / 2:
            raise ValueError(
                f"seize_freq must be >0 and below Nyquist, got {self.seize_freq}")
        if self.green_wink not in GREEN_WINKS:
            raise ValueError(
                f"green_wink must be one of {GREEN_WINKS}, got {self.green_wink!r}")
        for name in (
            "seize_duration",
            "wink_delay",
            "digit_duration",
            "inter_digit_gap",
            "kp_duration",
            "st_duration",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("coin_on", "coin_gap"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.coin_freqs is not None:
            if not self.coin_freqs:
                raise ValueError("coin_freqs must contain at least one frequency")
            for f in self.coin_freqs:
                if not 0 < f < self.sample_rate / 2:
                    raise ValueError(
                        f"coin_freqs entry {f} must be >0 and below Nyquist "
                        f"({self.sample_rate / 2:g} Hz)")


@dataclass
class Settings:
    """App-wide settings (not part of a sequence/preset)."""

    default_device: str | None = None
    theme: str = "phreakme"
    tui_refresh_rate: int = 30
    web_port: int = 8080
    web_host: str = "127.0.0.1"
    preset_dir: Path = field(default_factory=preset_dir)
    log_level: str = "info"
    log_file: Path | None = None
    defaults: Config = field(default_factory=Config)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_file()
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        audio = raw.get("audio", {})
        gen = raw.get("generation", {})
        ui = raw.get("ui", {})
        presets = raw.get("presets", {})
        logging_ = raw.get("logging", {})

        defaults = Config.from_dict(
            {
                **gen,
                "amplitude": audio.get("amplitude", Config.amplitude),
                "sample_rate": audio.get("sample_rate", Config.sample_rate),
            }
        )
        pdir = presets.get("default_directory")
        lfile = logging_.get("file")
        return cls(
            default_device=audio.get("default_device"),
            theme=ui.get("theme", cls.theme),
            tui_refresh_rate=ui.get("tui_refresh_rate", cls.tui_refresh_rate),
            web_port=ui.get("web_port", cls.web_port),
            web_host=ui.get("web_host", cls.web_host),
            preset_dir=Path(pdir).expanduser() if pdir else preset_dir(),
            log_level=logging_.get("level", cls.log_level),
            log_file=Path(lfile).expanduser() if lfile else None,
            defaults=defaults,
        )
