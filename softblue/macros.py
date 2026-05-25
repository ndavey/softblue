"""Macro storage: ordered chains of tone-sequence steps.

A *step* is either:
- inline: ``{"mode": ..., "digits": ..., "config": {...}, "delay_after": s}``
- preset reference: ``{"preset": "name", "delay_after": s}``

Storage layout mirrors :mod:`softblue.presets`: one JSON file per macro,
name-sanitised, under ``~/.softblue/macros/``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import config_dir

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def macro_dir() -> Path:
    return config_dir() / "macros"


class MacroError(ValueError):
    pass


def sanitize_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name) or name in (".", ".."):
        raise MacroError(
            f"invalid macro name {name!r} "
            "(use letters, digits, '.', '_', '-' only)"
        )
    return name


def validate_step(step: dict) -> dict:
    """Light schema check. Returns the step (untouched) or raises MacroError."""
    if not isinstance(step, dict):
        raise MacroError(f"step must be an object, got {type(step).__name__}")
    has_preset = "preset" in step
    has_inline = "digits" in step or "mode" in step
    if has_preset and has_inline:
        raise MacroError("step cannot have both 'preset' and inline 'mode'/'digits'")
    if not has_preset and not has_inline:
        raise MacroError("step must specify either 'preset' or 'mode'+'digits'")
    if has_preset and not isinstance(step["preset"], str):
        raise MacroError("'preset' must be a string")
    delay = step.get("delay_after", 0)
    if not isinstance(delay, (int, float)) or delay < 0:
        raise MacroError(f"delay_after must be a non-negative number, got {delay!r}")
    return step


class Macro:
    def __init__(
        self,
        name: str,
        steps: list[dict],
        description: str = "",
        pinned: bool = False,
        created_at: str | None = None,
    ):
        self.name = sanitize_name(name)
        self.steps = [validate_step(s) for s in (steps or [])]
        self.description = description
        self.pinned = bool(pinned)
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": self.steps,
            "pinned": self.pinned,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Macro":
        return cls(
            name=d["name"],
            steps=d.get("steps", []),
            description=d.get("description", ""),
            pinned=d.get("pinned", False),
            created_at=d.get("created_at"),
        )


class MacroManager:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or macro_dir())
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.directory / f"{sanitize_name(name)}.json"

    def list_all(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.directory.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def load(self, name: str) -> Macro:
        p = self._path(name)
        if not p.exists():
            raise MacroError(f"macro {name!r} not found")
        return Macro.from_dict(json.loads(p.read_text()))

    def save(self, macro: Macro) -> None:
        self._path(macro.name).write_text(json.dumps(macro.to_dict(), indent=2))

    def delete(self, name: str) -> None:
        p = self._path(name)
        if not p.exists():
            raise MacroError(f"macro {name!r} not found")
        p.unlink()
