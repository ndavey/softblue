"""Preset storage: one JSON file per preset, with name sanitisation."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, preset_dir

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

BUILTIN_PRESETS: list[dict[str, Any]] = [
    {"name": "projectmf-default", "description": "Standard ProjectMF dialing",
     "digits": "1234", "config": {"seize_duration": 2.0, "wink_delay": 0.5},
     "tags": ["projectmf", "default"]},
    {"name": "projectmf-slow", "description": "Fussy/older switches",
     "digits": "1234", "config": {"seize_duration": 3.0, "wink_delay": 1.0},
     "tags": ["projectmf"]},
    {"name": "seize-only", "description": "Just seize the trunk",
     "digits": "", "config": {"seize_duration": 2.0, "seize_only": True},
     "tags": ["seize"]},
    {"name": "contest-day", "description": "Conservative timing",
     "digits": "8675309", "config": {"seize_duration": 2.5, "wink_delay": 0.8},
     "tags": ["contest"]},
    {"name": "rapid-test", "description": "Quick testing",
     "digits": "0", "config": {"seize_duration": 1.0, "wink_delay": 0.3},
     "tags": ["test"]},
]


class PresetError(ValueError):
    pass


class Preset:
    def __init__(self, name: str, digits: str, config: Config,
                 description: str = "", tags: list[str] | None = None,
                 created_at: str | None = None):
        self.name = sanitize_name(name)
        self.digits = digits
        self.config = config
        self.description = description
        self.tags = tags or []
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "digits": self.digits,
            "config": self.config.to_dict(),
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Preset":
        return cls(
            name=d["name"],
            digits=d.get("digits", ""),
            config=Config.from_dict(d.get("config", {})),
            description=d.get("description", ""),
            tags=d.get("tags", []),
            created_at=d.get("created_at"),
        )


def sanitize_name(name: str) -> str:
    """Reject anything that could escape the preset directory."""
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name) or name in (".", ".."):
        raise PresetError(
            f"invalid preset name {name!r} "
            "(use letters, digits, '.', '_', '-' only)"
        )
    return name


class PresetManager:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or preset_dir())
        self.directory.mkdir(parents=True, exist_ok=True)
        self._seed_builtins()

    def _path(self, name: str) -> Path:
        return self.directory / f"{sanitize_name(name)}.json"

    def _seed_builtins(self) -> None:
        for spec in BUILTIN_PRESETS:
            p = self._path(spec["name"])
            if not p.exists():
                preset = Preset(
                    name=spec["name"],
                    digits=spec["digits"],
                    config=Config.from_dict(spec.get("config", {})),
                    description=spec.get("description", ""),
                    tags=spec.get("tags", []),
                )
                p.write_text(json.dumps(preset.to_dict(), indent=2))

    def list_all(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.directory.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def load(self, name: str) -> Preset:
        p = self._path(name)
        if not p.exists():
            raise PresetError(f"preset {name!r} not found")
        return Preset.from_dict(json.loads(p.read_text()))

    def save(self, preset: Preset) -> None:
        self._path(preset.name).write_text(json.dumps(preset.to_dict(), indent=2))

    def delete(self, name: str) -> None:
        p = self._path(name)
        if not p.exists():
            raise PresetError(f"preset {name!r} not found")
        p.unlink()
