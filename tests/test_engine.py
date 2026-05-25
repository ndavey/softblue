import numpy as np
import pytest

from softblue.config import Config
from softblue.engine import InvalidDigitError, ToneEngine


def dominant_freqs(samples, sample_rate, top=2):
    """Return the `top` strongest frequency bins in a tone segment."""
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / sample_rate)
    idx = np.argsort(spectrum)[-top:]
    return sorted(round(freqs[i] / 10) * 10 for i in idx)


@pytest.fixture
def engine():
    return ToneEngine()


def test_single_digit_frequency_content(engine):
    cfg = Config(seize_duration=0.0, wink_delay=0.0, digit_duration=0.2)
    for digit, (f1, f2) in ToneEngine.MF_DIGITS.items():
        tone = engine.generate_tone([f1, f2], 0.2, cfg.sample_rate, cfg.amplitude)
        assert dominant_freqs(tone, cfg.sample_rate) == sorted([f1, f2]), digit


def test_seize_tone_is_2600(engine):
    tone = engine.generate_tone([ToneEngine.SEIZURE_FREQ], 0.5, 8000, 0.7)
    peak = dominant_freqs(tone, 8000, top=1)[0]
    assert peak == 2600


def test_full_sequence_length(engine):
    cfg = Config()
    seq = engine.build_sequence("123", cfg)
    # seize + wink + KP + 3*(gap+digit) + gap + ST
    expected = cfg.seize_duration + cfg.wink_delay + cfg.kp_duration
    expected += 3 * (cfg.inter_digit_gap + cfg.digit_duration)
    expected += cfg.inter_digit_gap + cfg.st_duration
    assert len(seq) == pytest.approx(expected * cfg.sample_rate, abs=2)


def test_seize_only_mode(engine):
    cfg = Config(seize_only=True, seize_duration=1.0)
    seq = engine.build_sequence("", cfg)
    assert len(seq) == pytest.approx(1.0 * cfg.sample_rate, abs=2)
    assert dominant_freqs(seq, cfg.sample_rate, top=1)[0] == 2600


def test_invalid_digit_raises(engine):
    with pytest.raises(InvalidDigitError) as exc:
        engine.build_sequence("12@4", Config())
    assert exc.value.digit == "@"


def test_mf_control_chars_case_insensitive(engine):
    # KP/ST/seize/clear control chars (k/s/z/x) should accept either case.
    cfg = Config(seize_duration=0.05, kp_duration=0.05, st_duration=0.05,
                 digit_duration=0.05, inter_digit_gap=0.02)
    a = engine.build_sequence("zk123s", cfg)
    b = engine.build_sequence("ZK123S", cfg)
    assert len(a) == len(b) > 0
    np.testing.assert_array_equal(a, b)


def test_separators_allowed(engine):
    seq = engine.build_sequence("123-456 789", Config())
    assert len(seq) > 0


def test_wav_roundtrip(engine, tmp_path):
    cfg = Config(seize_duration=0.1)
    seq = engine.build_sequence("0", cfg)
    p = tmp_path / "out.wav"
    engine.write_wav(str(p), seq, cfg.sample_rate)
    data, sr = engine.read_wav(str(p))
    assert sr == cfg.sample_rate
    assert len(data) == len(seq)
    assert np.max(np.abs(data)) <= 1.0


def test_no_clipping(engine):
    seq = engine.build_sequence("1234567890", Config(amplitude=1.0))
    assert np.max(np.abs(seq)) <= 1.0 + 1e-6


def test_generate_chunks(engine):
    cfg = Config(seize_duration=0.1)
    chunks = list(engine.generate_chunks("1", cfg, chunk_ms=50))
    assert len(chunks) > 1
    assert all(len(c) % 2 == 0 for c in chunks)  # int16 = 2 bytes


# ---- mode dispatch -------------------------------------------------------

def test_mf_inline_skips_autowrap(engine):
    """Inline k/s in the digit string suppresses the default KP…ST wrap."""
    cfg = Config(seize_duration=0, wink_delay=0, inter_digit_gap=0,
                 digit_duration=0.05, kp_duration=0.05, st_duration=0.05)
    # 1 digit + 1 KP = 0.10s total. The auto-wrap path would add seize+ST etc.
    seq = engine.build_sequence("k1", cfg)
    assert len(seq) == pytest.approx(0.10 * cfg.sample_rate, abs=2)


def test_mf_inline_seize_uses_seize_duration(engine):
    cfg = Config(seize_duration=0.5, wink_delay=0, inter_digit_gap=0,
                 digit_duration=0.05)
    seq = engine.build_sequence("z", cfg)
    assert len(seq) == pytest.approx(0.5 * cfg.sample_rate, abs=2)
    assert dominant_freqs(seq, cfg.sample_rate, top=1)[0] == 2600


def test_c5_seize_is_2400(engine):
    cfg = Config(mode="c5", seize_duration=0.5, wink_delay=0, inter_digit_gap=0)
    seq = engine.build_sequence("z", cfg)
    assert dominant_freqs(seq, cfg.sample_rate, top=1)[0] == 2400


def test_dtmf_digit_frequencies(engine):
    # 8000 Hz is too coarse to round-trip DTMF row freqs (697 → 700 etc.);
    # bump the sample rate so the FFT bins resolve them exactly.
    cfg = Config(mode="dtmf", digit_duration=0.5, inter_digit_gap=0, sample_rate=16000)
    for digit, (f1, f2) in ToneEngine.DTMF_DIGITS.items():
        seq = engine.build_sequence(digit, cfg)
        peaks = dominant_freqs(seq, cfg.sample_rate)
        assert peaks[0] == pytest.approx(min(f1, f2), abs=10), digit
        assert peaks[1] == pytest.approx(max(f1, f2), abs=10), digit


def test_dtmf_accepts_letters_caseless(engine):
    cfg = Config(mode="dtmf", digit_duration=0.1, inter_digit_gap=0)
    assert len(engine.build_sequence("a", cfg)) > 0
    assert len(engine.build_sequence("A", cfg)) > 0


def test_dtmf_rejects_mf_only_chars(engine):
    with pytest.raises(InvalidDigitError) as exc:
        engine.build_sequence("k", Config(mode="dtmf"))
    assert exc.value.digit == "k"


def test_us_redbox_acts_uses_dual_tone(engine):
    cfg = Config(mode="us_redbox", coin_scheme="acts",
                 inter_digit_gap=0, sample_rate=16000)
    seq = engine.build_sequence("1", cfg)  # nickel: 66ms burst
    assert dominant_freqs(seq, cfg.sample_rate) == [1700, 2200]


def test_us_redbox_phreakme_is_single_tone(engine):
    cfg = Config(mode="us_redbox", coin_scheme="phreakme",
                 inter_digit_gap=0, sample_rate=16000)
    seq = engine.build_sequence("1", cfg)
    assert dominant_freqs(seq, cfg.sample_rate, top=1)[0] == 1700


def test_us_redbox_quarter_has_five_bursts(engine):
    cfg = Config(mode="us_redbox", coin_scheme="acts", inter_digit_gap=0)
    seq = engine.build_sequence("3", cfg)
    # 5 × 33ms tone + 4 × 33ms gap + 100ms trailing gap = 0.397s
    assert len(seq) == pytest.approx(0.397 * cfg.sample_rate, abs=2)


def test_uk_redbox_pip_durations(engine):
    cfg = Config(mode="uk_redbox", inter_digit_gap=0)
    seq_10p = engine.build_sequence("1", cfg)
    seq_50p = engine.build_sequence("2", cfg)
    assert len(seq_10p) == pytest.approx(0.200 * cfg.sample_rate, abs=2)
    assert len(seq_50p) == pytest.approx(0.350 * cfg.sample_rate, abs=2)
    assert dominant_freqs(seq_10p, cfg.sample_rate, top=1)[0] == 1000


def test_pulse_2600_digit_pulse_count(engine):
    """Digit N produces N pulses (0 = 10), each break=60ms + make=40ms = 100ms."""
    cfg = Config(mode="pulse_2600", inter_digit_gap=0)
    for digit, expected_pulses in [("3", 3), ("0", 10), ("7", 7)]:
        seq = engine.build_sequence(digit, cfg)
        expected_s = expected_pulses * 0.100
        assert len(seq) == pytest.approx(expected_s * cfg.sample_rate, abs=2), digit


def test_pulse_2600_rejects_non_digit(engine):
    with pytest.raises(InvalidDigitError):
        engine.build_sequence("A", Config(mode="pulse_2600"))


def test_invalid_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        Config(mode="foo").validate()


def test_invalid_coin_scheme_raises():
    with pytest.raises(ValueError, match="coin_scheme"):
        Config(coin_scheme="bogus").validate()


def test_old_preset_dict_still_loads():
    """Configs saved before mode/coin_scheme were added must still deserialize."""
    cfg = Config.from_dict({"seize_duration": 1.5, "amplitude": 0.5})
    assert cfg.mode == "mf_r1"
    assert cfg.coin_scheme == "acts"
    cfg.validate()
