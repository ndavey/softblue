import pytest

from softblue.config import Config
from softblue.presets import Preset, PresetError, PresetManager, sanitize_name


def test_sanitize_rejects_traversal():
    for bad in ("../etc/passwd", "..", ".", "a/b", "x\\y", "", "  "):
        with pytest.raises(PresetError):
            sanitize_name(bad)


def test_sanitize_accepts_normal():
    assert sanitize_name("projectmf-default") == "projectmf-default"


def test_builtins_seeded(tmp_path):
    mgr = PresetManager(tmp_path)
    names = {p["name"] for p in mgr.list_all()}
    assert {"projectmf-default", "seize-only", "rapid-test"} <= names


def test_seize_only_builtin_loads_and_builds(tmp_path):
    from softblue.engine import ToneEngine

    mgr = PresetManager(tmp_path)
    p = mgr.load("seize-only")
    assert p.config.seize_only is True
    seq = ToneEngine().build_sequence(p.digits, p.config)
    assert len(seq) > 0


def test_save_load_delete_roundtrip(tmp_path):
    mgr = PresetManager(tmp_path)
    mgr.save(Preset("mine", "123", Config(seize_duration=4.0), "desc"))
    loaded = mgr.load("mine")
    assert loaded.digits == "123"
    assert loaded.config.seize_duration == 4.0
    mgr.delete("mine")
    with pytest.raises(PresetError):
        mgr.load("mine")


def test_delete_missing_raises(tmp_path):
    with pytest.raises(PresetError):
        PresetManager(tmp_path).delete("nope")
