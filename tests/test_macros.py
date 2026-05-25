"""Macro engine + storage tests."""

from __future__ import annotations

import json

import numpy as np
import pytest

from softblue.config import Config
from softblue.engine import InvalidDigitError, ToneEngine
from softblue.macros import Macro, MacroError, MacroManager, sanitize_name
from softblue.presets import Preset, PresetManager


# ---- sanitisation -------------------------------------------------------

def test_sanitize_rejects_path_chars():
    for bad in ["", "..", "/x", "a b", "a/b", "."]:
        with pytest.raises(MacroError):
            sanitize_name(bad)


def test_sanitize_allows_normal_names():
    for ok in ["demo", "demo-1", "test.macro", "abc_123"]:
        assert sanitize_name(ok) == ok


# ---- schema -------------------------------------------------------------

def test_step_must_be_preset_or_inline():
    with pytest.raises(MacroError):
        Macro("m", [{}])
    with pytest.raises(MacroError):
        Macro("m", [{"preset": "p", "mode": "mf_r1"}])  # both


def test_step_delay_must_be_non_negative():
    with pytest.raises(MacroError):
        Macro("m", [{"mode": "mf_r1", "digits": "1", "delay_after": -1}])


# ---- manager round-trip -------------------------------------------------

def test_manager_save_load_delete(tmp_path):
    mgr = MacroManager(tmp_path)
    m = Macro("demo", [{"mode": "dtmf", "digits": "1"}], description="d", pinned=True)
    mgr.save(m)
    loaded = mgr.load("demo")
    assert loaded.steps == m.steps
    assert loaded.pinned is True
    assert {x["name"] for x in mgr.list_all()} == {"demo"}
    mgr.delete("demo")
    with pytest.raises(MacroError):
        mgr.load("demo")


def test_manager_load_missing(tmp_path):
    with pytest.raises(MacroError):
        MacroManager(tmp_path).load("nope")


# ---- engine build_macro -------------------------------------------------

def test_build_macro_inline_concat():
    eng = ToneEngine()
    base = Config(seize_duration=0, wink_delay=0, inter_digit_gap=0,
                  digit_duration=0.1, kp_duration=0.1, st_duration=0.1)
    steps = [
        {"mode": "dtmf", "digits": "1", "delay_after": 0.2},
        {"mode": "dtmf", "digits": "2"},
    ]
    seq = eng.build_macro(steps, base)
    # 0.1 + 0.2 + 0.1 = 0.4
    assert len(seq) == pytest.approx(0.4 * base.sample_rate, abs=2)


def test_build_macro_step_overrides_mode_and_coin_scheme():
    """Each step's config overrides the base; the base mode (mf_r1) is ignored."""
    eng = ToneEngine()
    base = Config(seize_duration=0, wink_delay=0, inter_digit_gap=0,
                  kp_duration=0, st_duration=0, sample_rate=16000)
    steps = [{"mode": "us_redbox", "digits": "1",
              "config": {"coin_scheme": "phreakme"}}]
    seq = eng.build_macro(steps, base)
    # 1 nickel burst = 66ms tone + 100ms trailing gap
    assert len(seq) == pytest.approx(0.166 * 16000, abs=2)


def test_build_macro_preset_reference(tmp_path):
    eng = ToneEngine()
    pmgr = PresetManager(tmp_path)
    pmgr.save(Preset("p", "1", Config(seize_duration=0, wink_delay=0,
                                       inter_digit_gap=0, kp_duration=0.1,
                                       digit_duration=0.1, st_duration=0.1)))
    base = Config()
    steps = [{"preset": "p"}]
    seq = eng.build_macro(steps, base, preset_lookup=pmgr.load)
    # KP (0.1) + gap (0) + digit (0.1) + gap (0) + ST (0.1) = 0.3s
    assert len(seq) == pytest.approx(0.3 * base.sample_rate, abs=2)


def test_build_macro_preset_requires_lookup():
    eng = ToneEngine()
    with pytest.raises(Exception):
        eng.build_macro([{"preset": "x"}], Config())


def test_build_macro_propagates_invalid_digit():
    eng = ToneEngine()
    with pytest.raises(InvalidDigitError):
        eng.build_macro([{"mode": "dtmf", "digits": "k"}], Config())


def test_macro_json_round_trip(tmp_path):
    mgr = MacroManager(tmp_path)
    m = Macro("r", [
        {"mode": "mf_r1", "digits": "k1234s", "delay_after": 0.5},
        {"preset": "p", "delay_after": 0.0},
    ])
    mgr.save(m)
    raw = json.loads((tmp_path / "r.json").read_text())
    assert raw["steps"][0]["digits"] == "k1234s"
    assert raw["steps"][1]["preset"] == "p"
