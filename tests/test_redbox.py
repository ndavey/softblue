"""Red box scheme parameterisation and sweep scoring."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from softblue.config import Config
from softblue.engine import ToneEngine
from softblue.redbox import (
    KNOWN_A,
    KNOWN_B,
    MF_ALPHABET,
    RANKED_PATH,
    RedboxScheme,
    candidates,
    default_candidates,
    fingerprint,
    load_ranked,
    response_changed,
)
from softblue.sweep import scan_segments


@pytest.fixture
def engine():
    return ToneEngine()


def render(engine, scheme, symbol):
    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0,
                 coin_spec=scheme.coin_spec())
    return engine.build_sequence(symbol, cfg)


# ---- the parameterisation ----------------------------------------------------


def test_default_scheme_reproduces_last_years_table(engine):
    """The control must be byte-identical to the built-in PhreakMe table."""
    scheme = RedboxScheme()
    assert scheme.is_control
    for sym in "ndq$cr":
        via_scheme = render(engine, scheme, sym)
        builtin = engine.build_sequence(
            sym, Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0))
        assert len(via_scheme) == len(builtin), sym
        assert float(np.max(np.abs(via_scheme - builtin))) < 1e-6, sym


def test_scheme_moves_only_the_frequencies(engine):
    """Changing the pair must preserve structure: 2 tones, 60ms, same order."""
    scheme = RedboxScheme(freq_a=1300, freq_b=900)
    segs = [s for s in scan_segments(render(engine, scheme, "q"), 8000)
            if not s.silent]
    assert len(segs) == 2
    assert segs[0].freqs[0] == pytest.approx(1300, abs=25)
    assert segs[1].freqs[0] == pytest.approx(900, abs=25)
    assert all(s.dur_ms == pytest.approx(60, abs=3) for s in segs)


def test_nickel_and_dime_keep_their_level_split(engine):
    """Level stays semantic whatever the frequency is."""
    scheme = RedboxScheme(freq_a=1100, freq_b=1500)
    n = render(engine, scheme, "n")
    d = render(engine, scheme, "d")
    split = 20 * np.log10(float(np.max(np.abs(d))) / float(np.max(np.abs(n))))
    assert split == pytest.approx(3.0, abs=0.2)


def test_dollar_is_both_tones_together(engine):
    scheme = RedboxScheme(freq_a=700, freq_b=1500)
    segs = [s for s in scan_segments(render(engine, scheme, "$"), 8000)
            if not s.silent]
    assert len(segs) == 1
    assert [round(f / 50) * 50 for f in segs[0].freqs] == [700, 1500]


def test_collect_and_return_pulse_counts(engine):
    scheme = RedboxScheme(freq_a=900, freq_b=1300)
    coll = [s for s in scan_segments(render(engine, scheme, "c"), 8000) if not s.silent]
    assert len(coll) == 3
    ret = render(engine, scheme, "r")
    # 6 pulses with tight 15ms gaps.
    assert len(ret) == pytest.approx((6 * 0.060 + 6 * 0.015) * 8000, abs=4)


# ---- the search space --------------------------------------------------------


def test_candidates_cover_every_ordered_pair():
    c = candidates()
    n = len(MF_ALPHABET)
    assert len(c) == n * (n - 1)
    pairs = {(s.freq_a, s.freq_b) for s in c}
    assert len(pairs) == n * (n - 1)
    assert all(s.freq_a != s.freq_b for s in c)


def test_control_is_swept_first():
    """The sweep needs a baseline before it can call anything a hit."""
    assert candidates()[0].is_control
    assert (candidates()[0].freq_a, candidates()[0].freq_b) == (KNOWN_A, KNOWN_B)


def test_control_can_be_excluded():
    assert not any(s.is_control for s in candidates(include_control=False))


def test_reversal_ranks_second():
    assert (candidates()[1].freq_a, candidates()[1].freq_b) == (KNOWN_B, KNOWN_A)


def test_durations_multiply_the_space():
    assert len(candidates(durations=(0.060, 0.080))) == 2 * len(candidates())


def test_ranked_json_drops_undetectable_frequencies(tmp_path):
    """A pair the server's Goertzel cannot measure cannot be the answer."""
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"ranked": [
        {"freq_a": 1700, "freq_b": 2200, "duration_ms": 60, "rationale": "control"},
        {"freq_a": 1234, "freq_b": 2200, "duration_ms": 60, "rationale": "bogus"},
        {"freq_a": 900, "freq_b": 1500, "duration_ms": 80, "rationale": "plausible"},
    ]}))
    got = load_ranked(p)
    assert [(s.freq_a, s.freq_b) for s in got] == [(1700, 2200), (900, 1500)]
    assert got[1].duration == pytest.approx(0.080)


def test_ranked_json_carries_every_axis(tmp_path):
    """Off-frequency hypotheses are the point of a ranked list, not noise."""
    p = tmp_path / "r.json"
    p.write_text(json.dumps([
        {"freq_a": 1700, "freq_b": 2200, "duration_ms": 66, "gap_ms": 66,
         "return_gap_ms": 30, "nickel_dbfs": -9, "dime_dbfs": -3,
         "level_dbfs": -6, "confidence": "low"},
    ]))
    s = load_ranked(p)[0]
    assert s.duration == pytest.approx(0.066)
    assert s.gap == pytest.approx(0.066)
    assert s.return_gap == pytest.approx(0.030)
    assert (s.nickel_dbfs, s.dime_dbfs, s.level_dbfs) == (-9.0, -3.0, -6.0)
    assert s.confidence == "low"
    assert not s.is_control


# ---- scheme identity ---------------------------------------------------------
#
# Names are the sweep's resume key and its log key. A collision means a
# candidate is never dialled and a hit cannot be traced back to what produced
# it — both silent, both only discoverable at the con.


def test_same_pair_different_experiment_gets_a_different_name():
    control = RedboxScheme()
    slower = RedboxScheme(duration=0.066)
    inverted = RedboxScheme(nickel_dbfs=-3.0, dime_dbfs=-6.0)
    names = {control.name, slower.name, inverted.name}
    assert len(names) == 3, names


def test_only_last_years_exact_table_is_the_control():
    assert RedboxScheme().is_control
    # Same pair, but each of these is a prediction about *this* year.
    assert not RedboxScheme(duration=0.066).is_control
    assert not RedboxScheme(gap=0.066).is_control
    assert not RedboxScheme(return_gap=0.030).is_control
    assert not RedboxScheme(nickel_dbfs=-3.0, dime_dbfs=-6.0).is_control
    assert not RedboxScheme(level_dbfs=-6.0).is_control
    assert "control" not in RedboxScheme(nickel_dbfs=-9.0).describe()


def test_duplicate_entries_are_still_told_apart(tmp_path):
    """Even a file that repeats itself must not collapse two sweep slots."""
    p = tmp_path / "r.json"
    entry = {"freq_a": 1700, "freq_b": 2200, "duration_ms": 60}
    p.write_text(json.dumps([entry, dict(entry), {**entry, "label": "custom"},
                             {**entry, "label": "custom"}]))
    names = [s.name for s in load_ranked(p)]
    assert len(set(names)) == len(names), names


def test_scheme_survives_a_dict_round_trip():
    s = RedboxScheme(freq_a=900, freq_b=1300, duration=0.066, gap=0.066,
                     return_gap=0.030, nickel_dbfs=-9.0, level_dbfs=-6.0)
    back = RedboxScheme.from_dict(s.to_dict())
    assert back.coin_spec() == s.coin_spec()
    assert back.name == s.name


# ---- the shipped ranking -----------------------------------------------------


def test_shipped_ranking_is_present_and_unambiguous():
    schemes = load_ranked(RANKED_PATH)
    assert len(schemes) >= 15
    names = [s.name for s in schemes]
    assert len(set(names)) == len(names), "two candidates share a sweep key"
    assert schemes[0].is_control, "the baseline has to be dialled first"
    assert sum(s.is_control for s in schemes) == 1


def test_default_candidates_ranks_first_then_covers_the_rest():
    d = default_candidates()
    n = len(MF_ALPHABET)
    assert d[0].is_control
    assert {(s.freq_a, s.freq_b) for s in d} == {
        (a, b) for a in MF_ALPHABET for b in MF_ALPHABET if a != b}
    assert len(d) >= n * (n - 1)
    names = [s.name for s in d]
    assert len(set(names)) == len(names)
    # The argued candidates lead; the bare enumeration follows.
    assert (d[1].freq_a, d[1].freq_b) == (1500, 2200)


def test_default_candidates_falls_back_when_the_file_is_gone(tmp_path):
    assert default_candidates(tmp_path / "missing.json") == candidates()


def test_default_candidates_falls_back_on_a_corrupt_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert default_candidates(p) == candidates()


# ---- browser / server parity -------------------------------------------------
#
# The page synthesises coins itself so it keeps working with no backend, which
# means the scheme constants exist twice. If they drift, the preview held
# against the handset and the tones a SIP call actually sends stop agreeing —
# and the sweep would then be scoring the wrong thing without saying so.


def _js_source() -> str:
    import softblue
    return (Path(softblue.__file__).parent / "static" / "tone-engine.js").read_text()


def test_browser_alphabet_matches_python():
    m = re.search(r"const MF_ALPHABET = (\[[^\]]*\]);", _js_source())
    assert m, "MF_ALPHABET not found in tone-engine.js"
    assert tuple(json.loads(m.group(1))) == MF_ALPHABET


def test_browser_known_pair_matches_python():
    m = re.search(r"const KNOWN_A = (\d+), KNOWN_B = (\d+);", _js_source())
    assert m, "KNOWN_A/KNOWN_B not found in tone-engine.js"
    assert (int(m.group(1)), int(m.group(2))) == (KNOWN_A, KNOWN_B)


def test_browser_control_defaults_match_python():
    body = re.search(r"const CONTROL = \{(.*?)\};", _js_source(), re.S)
    assert body, "CONTROL not found in tone-engine.js"
    js = {k: float(v) for k, v in
          re.findall(r"(\w+):\s*(-?[\d.]+)", body.group(1))}
    control = RedboxScheme()
    assert js == {
        "duration": control.duration, "gap": control.gap,
        "return_gap": control.return_gap, "nickel_dbfs": control.nickel_dbfs,
        "dime_dbfs": control.dime_dbfs, "level_dbfs": control.level_dbfs,
    }


# ---- response scoring --------------------------------------------------------


def test_identical_audio_is_not_a_change():
    t = np.arange(8000) / 8000
    a = np.sin(2 * np.pi * 400 * t).astype(np.float32)
    changed, dist = response_changed(fingerprint(a), fingerprint(a))
    assert not changed and dist == pytest.approx(0.0, abs=1e-6)


def test_a_different_prompt_registers_as_changed():
    t = np.arange(8000) / 8000
    a = np.sin(2 * np.pi * 400 * t).astype(np.float32)
    b = np.sin(2 * np.pi * 2000 * t).astype(np.float32)
    changed, dist = response_changed(fingerprint(a), fingerprint(b))
    assert changed and dist > 1.0


def test_silence_against_audio_is_handled():
    changed, dist = response_changed(
        fingerprint(np.zeros(0, dtype=np.float32)),
        fingerprint(np.ones(800, dtype=np.float32) * 0.1))
    assert not changed          # empty baseline cannot be compared, not a crash


def test_fingerprint_is_serialisable():
    t = np.arange(800) / 8000
    fp = fingerprint(np.sin(2 * np.pi * 700 * t).astype(np.float32))
    d = fp.to_dict()
    assert set(d) == {"duration", "energy", "speech_ratio", "tone_ratio"}
    json.dumps(d)
