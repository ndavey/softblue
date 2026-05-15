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
        engine.build_sequence("12X4", Config())
    assert exc.value.digit == "X"


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
