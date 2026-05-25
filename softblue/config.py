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
    # US red-box coin scheme — "acts" (real Bell 1700+2200) or "phreakme" (1700 only).
    coin_scheme: str = "acts"

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
        from .engine import COIN_SCHEMES, MODES

        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.coin_scheme not in COIN_SCHEMES:
            raise ValueError(
                f"coin_scheme must be one of {COIN_SCHEMES}, got {self.coin_scheme!r}")
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
