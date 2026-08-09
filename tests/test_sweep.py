"""Tests for black-box red-box probing: candidate grid and acoustic modelling."""

from __future__ import annotations

import numpy as np
import pytest

from softblue.config import Config
from softblue.engine import ToneEngine
from softblue.sweep import (
    FREQ_SETS,
    TIMING_PROFILES,
    AcousticPath,
    CoinProbe,
    _envelope,
    burst_report,
    candidates,
    scan_segments,
    survivable,
)


@pytest.fixture
def engine():
    return ToneEngine()


@pytest.fixture
def base():
    return Config(mode="us_redbox", sample_rate=8000)


# ---- engine parameterisation -------------------------------------------------


def dominant(seq, sr, top=2):
    """Peak frequencies, to the nearest FFT bin (~15Hz at these window sizes)."""
    spec = np.abs(np.fft.rfft(seq * np.hanning(len(seq))))
    freqs = np.fft.rfftfreq(len(seq), 1 / sr)
    return sorted(round(float(freqs[i])) for i in np.argsort(spec)[-top:])


def assert_tone_at(seq, sr, target, tol=20.0):
    got = dominant(seq, sr, top=1)[0]
    assert abs(got - target) <= tol, f"expected ~{target}Hz, got {got}Hz"


def test_nortel_scheme_is_2200_only(engine):
    cfg = Config(mode="us_redbox", coin_scheme="nortel",
                 inter_digit_gap=0, sample_rate=16000)
    seq = engine.build_sequence("1", cfg)
    assert_tone_at(seq, cfg.sample_rate, 2200)


def test_coin_freqs_override_beats_scheme(engine):
    cfg = Config(mode="us_redbox", coin_scheme="acts", coin_freqs=[1300.0],
                 inter_digit_gap=0, sample_rate=16000)
    seq = engine.build_sequence("1", cfg)
    assert_tone_at(seq, cfg.sample_rate, 1300)


def test_coin_on_and_gap_overrides_change_length(engine):
    """Quarter = 5 pulses + 4 gaps + trailing silence."""
    cfg = Config(mode="us_redbox", coin_on=0.050, coin_gap=0.050,
                 inter_digit_gap=0, sample_rate=8000)
    seq = engine.build_sequence("3", cfg)
    expected = (5 * 0.050) + (4 * 0.050) + ToneEngine.COIN_TRAIL_S
    assert len(seq) == pytest.approx(expected * cfg.sample_rate, abs=2)


def test_default_coin_timing_unchanged(engine):
    """Regression: parameterising the coins must not move the defaults."""
    cfg = Config(mode="us_redbox", coin_scheme="acts", inter_digit_gap=0)
    seq = engine.build_sequence("3", cfg)
    assert len(seq) == pytest.approx(0.397 * cfg.sample_rate, abs=2)


def test_short_burst_keeps_its_energy(engine):
    """Regression: a fixed 5ms fade used to gut the 33ms quarter pulse.

    Ramping both edges of a 33ms burst by 5ms leaves under 80% of the burst
    energy and only ~23ms at level — under the on-time floor of a detector
    expecting 33ms, which is enough to make quarters silently fail.
    """
    sr, dur, amp = 8000, 0.033, 0.7
    burst = engine.generate_tone([1700, 2200], dur, sr, amp)
    ideal = amp**2 / 2 / 2 * len(burst)  # two summed sines scaled by 1/N
    assert float(np.sum(burst.astype(np.float64) ** 2)) / ideal > 0.90


def test_long_tone_still_gets_full_fade(engine):
    """The 5% cap must not shorten fades on ordinary-length tones."""
    sr = 8000
    burst = engine.generate_tone([1700], 2.0, sr, 0.7)
    fade = int(sr * 0.005)
    assert abs(float(burst[0])) < 1e-6                  # starts at zero
    assert float(np.max(np.abs(burst[:fade]))) < 0.7    # still ramping


# ---- candidate grid ----------------------------------------------------------


def test_candidates_cover_the_grid():
    probes = candidates(coin="3")
    assert len(probes) == (len(FREQ_SETS) - 1) * len(TIMING_PROFILES)
    assert probes[0].freqs == (1700.0, 2200.0)          # ACTS first
    assert probes[0].timing_label == "acts-canonical"


def test_candidates_multiply_by_amplitude():
    probes = candidates(coin="3", amplitudes=(0.5, 0.9))
    assert len(probes) == (len(FREQ_SETS) - 1) * len(TIMING_PROFILES) * 2
    assert {p.amplitude for p in probes} == {0.5, 0.9}


def test_probe_renders_requested_shape(base):
    p = CoinProbe(coin="3", scheme="nortel", freqs=(2200.0,), on_s=0.05, gap_s=0.05)
    seq = p.render(base)
    expected = (5 * 0.05) + (4 * 0.05) + ToneEngine.COIN_TRAIL_S
    assert len(seq) == pytest.approx(expected * base.sample_rate, abs=2)
    assert_tone_at(seq, base.sample_rate, 2200)


# ---- envelope / burst analysis -----------------------------------------------


def test_envelope_does_not_alias_at_2200hz():
    """Regression: rectify-and-smooth aliases 2200Hz's harmonic past Nyquist.

    At sr=8000 the second harmonic of 2200Hz lands at 4400Hz, folding back as a
    spurious beat that reads as amplitude ripple and corrupts burst counting.
    """
    sr = 8000
    t = np.arange(int(sr * 0.2)) / sr
    tone = 0.7 * np.sin(2 * np.pi * 2200 * t)
    env = _envelope(tone, sr)
    mid = env[len(env) // 4 : -len(env) // 4]
    assert float(np.std(mid) / np.mean(mid)) < 0.05     # flat, not beating


def test_clean_path_resolves_every_pulse(base):
    p = CoinProbe(coin="3", scheme="acts", freqs=(1700.0, 2200.0),
                  on_s=0.033, gap_s=0.033)
    clean = AcousticPath(wet=0.0, agc_ratio=1.0, noise_db=-60.0)
    r = survivable(p, base, clean)
    assert r.bursts == 5 and r.intact
    assert r.gap_depth_db < -30.0


def test_gap_depth_degrades_monotonically_with_reverb(base):
    """More room in the path must fill the gaps, never deepen them."""
    p = CoinProbe(coin="3", scheme="acts", freqs=(1700.0, 2200.0),
                  on_s=0.033, gap_s=0.033)
    depths = [survivable(p, base, AcousticPath(rt60=0.35, wet=w)).gap_depth_db
              for w in (0.0, 0.05, 0.15, 0.30, 0.45)]
    assert depths == sorted(depths), depths


def test_burst_report_counts_a_hand_built_train():
    sr = 8000
    on, gap = int(sr * 0.05), int(sr * 0.05)
    t = np.arange(on) / sr
    pulse = np.sin(2 * np.pi * 1700 * t)
    sig = np.concatenate(
        sum(([pulse, np.zeros(gap)] for _ in range(5)), [])
    )
    r = burst_report(sig, sr, expected=5, freqs=(1700.0,))
    assert r.bursts == 5 and r.intact
    assert r.mean_on_ms == pytest.approx(50, abs=6)


# ---- PhreakMe coin scheme ----------------------------------------------------


def test_phreakme_nickel_and_dime_differ_only_by_level(engine):
    """Level is the sole distinguishing feature — 3 dB apart, same tone."""
    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0)
    n = engine.build_sequence("n", cfg)
    d = engine.build_sequence("d", cfg)
    assert len(n) == len(d)
    ratio = 20 * np.log10(float(np.max(np.abs(d))) / float(np.max(np.abs(n))))
    assert ratio == pytest.approx(3.0, abs=0.2)


def test_phreakme_quarter_is_a_1700_then_2200_sequence(engine):
    """Not a burst count: two different tones in order, 60ms each."""
    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0)
    segs = [s for s in scan_segments(engine.build_sequence("q", cfg), 8000)
            if not s.silent]
    assert len(segs) == 2
    assert segs[0].freqs[0] == pytest.approx(1700, abs=20)
    assert segs[1].freqs[0] == pytest.approx(2200, abs=20)
    assert all(s.dur_ms == pytest.approx(60, abs=3) for s in segs)


def test_phreakme_dollar_is_simultaneous(engine):
    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0)
    segs = [s for s in scan_segments(engine.build_sequence("$", cfg), 8000)
            if not s.silent]
    assert len(segs) == 1
    assert [round(f / 10) * 10 for f in segs[0].freqs] == [1700, 2200]


def test_analyze_recovers_levels_exactly(engine):
    """Detection must not drift on level, since level carries meaning here."""
    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0)
    for sym, want in (("n", -6.0), ("d", -3.0)):
        segs = [s for s in scan_segments(engine.build_sequence(sym, cfg), 8000)
                if not s.silent]
        assert segs[0].level_dbfs == pytest.approx(want, abs=0.2)


def test_discovered_spec_replays_the_same_tone(engine):
    """analyze -> spec -> play must reproduce what was scanned."""
    from softblue.sweep import spec_from_segments

    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0)
    original = engine.build_sequence("q", cfg)
    spec = spec_from_segments(scan_segments(original, 8000))
    replay = engine.build_sequence("q", cfg.merged(coin_spec={"q": spec}))
    assert len(replay) == len(original)
    assert float(np.max(np.abs(replay - original))) < 0.02


def test_coin_spec_accepts_symbols_outside_the_builtin_table(engine):
    """A rediscovered scheme may name coins the built-in table has never seen."""
    cfg = Config(mode="phreakme_coin", sample_rate=8000, inter_digit_gap=0,
                 coin_spec={"Z": [[[1234], 0.05, -3.0], [None, 0.05, 0.0]]})
    seq = engine.build_sequence("Z", cfg)
    assert len(seq) == pytest.approx(0.1 * 8000, abs=2)
    assert_tone_at(seq, 8000, 1234, tol=30)


def test_phreakme_grid_leads_with_last_known_good():
    from softblue.sweep import candidates_phreakme

    probes = candidates_phreakme(symbol="q")
    assert probes[0].label.startswith("1700->2200Hz 60ms")
    assert all(p.symbol == "q" for p in probes)


# ---- MF variant / seize override (browser-server parity) ---------------------


def test_coin_mf_variant_uses_phreakme_kp_st(engine):
    """The UI has always sent mf_variant; the server used to ignore it."""
    cfg = Config(mode="mf_r1", mf_variant="coin", sample_rate=16000,
                 seize_duration=0, wink_delay=0, inter_digit_gap=0,
                 kp_duration=0.1, st_duration=0)
    kp = engine.build_sequence("k", cfg.merged(digit_duration=0.1))
    assert sorted(dominant(kp, 16000)) == [1700, 2200]


def test_standard_mf_variant_is_unchanged(engine):
    cfg = Config(mode="mf_r1", sample_rate=16000, seize_duration=0,
                 wink_delay=0, inter_digit_gap=0, kp_duration=0.1)
    kp = engine.build_sequence("k", cfg)
    got = sorted(dominant(kp, 16000))
    assert abs(got[0] - 1100) < 20 and abs(got[1] - 1700) < 20


def test_seize_freq_override_is_honoured(engine):
    cfg = Config(mode="mf_r1", seize_only=True, seize_duration=0.2,
                 seize_freq=1234, sample_rate=16000)
    assert_tone_at(engine.build_sequence("", cfg), 16000, 1234)


def test_invalid_mf_variant_and_seize_freq_rejected():
    with pytest.raises(ValueError, match="mf_variant"):
        Config(mode="mf_r1", mf_variant="bogus").validate()
    with pytest.raises(ValueError, match="seize_freq"):
        Config(mode="mf_r1", seize_freq=9000, sample_rate=8000).validate()
