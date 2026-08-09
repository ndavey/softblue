"""Black-box red-box probing: candidate grids and acoustic-path modelling.

Two independent questions have to be answered when a coin challenge won't
respond, and conflating them is what makes the search feel hopeless:

1. *What does the target want?*  Answered by :func:`candidates` — an ordered
   grid of plausible (frequency set, on-time, gap) triples, most-likely first.
2. *What can your speaker actually deliver?*  Answered by :class:`AcousticPath`
   plus :func:`burst_report` — feed a candidate through a model of
   speaker → room → handset mic and check whether the pulse train is still
   resolvable at the far end.

A candidate that fails (2) can never satisfy (1) no matter how many times you
replay it, so pre-filtering the grid against the acoustic model is usually the
difference between a 15-attempt search and an unbounded one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .engine import ToneEngine

# Telephone passband. Anything outside this is gone by the time it reaches the
# far-end detector, whichever codec the carrier happens to use.
TELEPHONE_BAND_HZ = (300.0, 3400.0)

# Frequency sets worth trying, in descending order of prior likelihood.
FREQ_SETS: list[tuple[str, tuple[float, ...]]] = [
    ("acts", (1700.0, 2200.0)),      # Bell ACTS dual tone — the standard
    ("nortel", (2200.0,)),           # Canadian / Nortel single tone
    ("phreakme", (1700.0,)),         # single 1700 Hz
    ("acts-swapped", (2200.0, 1700.0)),  # identical spectrally; kept for logging parity
]

# (on_seconds, gap_seconds) profiles for the quarter, most-likely first. The
# relaxed entries exist because a detector with loose tolerances will accept a
# slower pulse train, and a slower train survives a room far better.
TIMING_PROFILES: list[tuple[str, float, float]] = [
    ("acts-canonical", 0.033, 0.033),
    ("nortel", 0.035, 0.035),
    ("relaxed-50", 0.050, 0.050),
    ("relaxed-66", 0.066, 0.066),
    ("slow-100", 0.100, 0.100),
]


@dataclass
class CoinProbe:
    """One point in the search space."""

    coin: str
    scheme: str
    freqs: tuple[float, ...]
    on_s: float
    gap_s: float
    amplitude: float = 0.7
    timing_label: str = ""

    @property
    def label(self) -> str:
        f = "+".join(f"{int(x)}" for x in self.freqs)
        return (f"{f}Hz on={self.on_s * 1000:.0f}ms gap={self.gap_s * 1000:.0f}ms "
                f"amp={self.amplitude:.2f}")

    def to_config(self, base: Config) -> Config:
        return base.merged(
            mode="us_redbox",
            coin_scheme=self.scheme if self.scheme in ("acts", "nortel", "phreakme")
            else "acts",
            coin_freqs=list(self.freqs),
            coin_on=self.on_s,
            coin_gap=self.gap_s,
            amplitude=self.amplitude,
        )

    def render(self, base: Config) -> np.ndarray:
        cfg = self.to_config(base)
        return ToneEngine().build_sequence(self.coin, cfg)

    def to_dict(self) -> dict:
        return {
            "coin": self.coin, "scheme": self.scheme, "freqs": list(self.freqs),
            "on_s": self.on_s, "gap_s": self.gap_s, "amplitude": self.amplitude,
            "timing_label": self.timing_label, "label": self.label,
        }


def candidates(
    coin: str = "3",
    amplitudes: tuple[float, ...] = (0.7,),
    include_swapped: bool = False,
) -> list[CoinProbe]:
    """Ordered candidate grid for one coin, most-likely first.

    The ordering is by prior likelihood, not by acoustic survivability — pair it
    with :func:`survivable` to drop the ones your speaker cannot deliver.
    """
    out: list[CoinProbe] = []
    for scheme, freqs in FREQ_SETS:
        if scheme == "acts-swapped" and not include_swapped:
            continue
        for tlabel, on_s, gap_s in TIMING_PROFILES:
            for amp in amplitudes:
                out.append(CoinProbe(coin=coin, scheme=scheme, freqs=freqs,
                                     on_s=on_s, gap_s=gap_s, amplitude=amp,
                                     timing_label=tlabel))
    return out


# ---- PhreakMe-shaped candidates ----------------------------------------------

# PhreakMe builds coins from single tones at MF-family frequencies, so a changed
# scheme most likely re-drew from this pool rather than inventing new tones.
#
# The pool is the *whole* alphabet the server's own Goertzel can measure
# (_MF_ALL_FREQS), not a hand-picked subset: a narrower pool silently deletes
# candidates from the sweep, and the four it used to omit (700, 900, 1100 and
# the pairs built on them) include the collision-free pick that is the leading
# hypothesis if the "keep 2200 as the guard" family all miss.
PHREAKME_FREQ_POOL: tuple[float, ...] = (700.0, 900.0, 1100.0, 1300.0,
                                         1500.0, 1700.0, 2200.0)
PHREAKME_DURATIONS: tuple[float, ...] = (0.060, 0.080)
PHREAKME_LEVELS: tuple[float, ...] = (-3.0,)


@dataclass
class SpecProbe:
    """A candidate PhreakMe coin pattern, rendered from an explicit segment list."""

    symbol: str
    segments: list
    label: str = ""
    timing_label: str = "phreakme"

    def to_config(self, base: Config) -> Config:
        return base.merged(mode="phreakme_coin",
                           coin_spec={self.symbol: self.segments})

    def render(self, base: Config) -> np.ndarray:
        return ToneEngine().build_sequence(self.symbol, self.to_config(base))

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "segments": self.segments, "label": self.label}


def candidates_phreakme(
    symbol: str = "q",
    freqs: tuple[float, ...] = PHREAKME_FREQ_POOL,
    durations: tuple[float, ...] = PHREAKME_DURATIONS,
    levels: tuple[float, ...] = PHREAKME_LEVELS,
    two_tone: bool = True,
) -> list[SpecProbe]:
    """Grid over the PhreakMe coin shape: an ordered tone pair, or a single tone.

    The known quarter is 1700→2200 at 60ms, so that ordering leads the list.
    """
    out: list[SpecProbe] = []
    for dur in durations:
        for level in levels:
            if two_tone:
                for f1 in freqs:
                    for f2 in freqs:
                        if f1 == f2:
                            continue
                        segs = [[[f1], dur, level], [None, dur, 0.0],
                                [[f2], dur, level], [None, dur, 0.0]]
                        out.append(SpecProbe(symbol, segs,
                                             f"{f1:.0f}->{f2:.0f}Hz "
                                             f"{dur * 1000:.0f}ms {level:.0f}dBFS"))
            else:
                for f1 in freqs:
                    segs = [[[f1], dur, level], [None, dur, 0.0]]
                    out.append(SpecProbe(symbol, segs,
                                         f"{f1:.0f}Hz {dur * 1000:.0f}ms "
                                         f"{level:.0f}dBFS"))
    # Lead with the last-known-good shape if it is present in the grid.
    known = f"1700->2200Hz 60ms -3dBFS"
    out.sort(key=lambda p: p.label != known)
    return out


# ---- acoustic path -----------------------------------------------------------


@dataclass
class AcousticPath:
    """Speaker → room → handset-mic model.

    ``rt60`` is the room's 60 dB decay time and ``wet`` the reverberant energy
    relative to the direct path; pressing the speaker against the mouthpiece is
    roughly ``wet=0.05``, a phone held a foot away in a hard-walled room is
    ``wet=0.5`` or worse. ``agc_ratio`` models the compression every handset and
    softphone applies, which flattens the pulse train's dynamics.
    """

    rt60: float = 0.35
    wet: float = 0.35
    agc_ratio: float = 4.0
    agc_attack_ms: float = 5.0
    agc_release_ms: float = 200.0
    noise_db: float = -45.0
    band: tuple[float, float] = TELEPHONE_BAND_HZ
    seed: int = 1234

    def apply(self, x: np.ndarray, sr: int) -> np.ndarray:
        y = self._reverb(x, sr)
        y = self._bandpass(y, sr)
        y = self._agc(y, sr)
        y = self._noise(y)
        peak = float(np.max(np.abs(y))) or 1.0
        return (y / peak * 0.9).astype(np.float32)

    def _reverb(self, x: np.ndarray, sr: int) -> np.ndarray:
        if self.wet <= 0 or self.rt60 <= 0:
            return x.astype(np.float64)
        n = int(sr * self.rt60)
        if n < 2:
            return x.astype(np.float64)
        rng = np.random.default_rng(self.seed)
        t = np.arange(n) / sr
        # -60 dB at t == rt60.
        ir = rng.standard_normal(n) * 10.0 ** (-3.0 * t / self.rt60)
        ir /= np.sqrt(np.sum(ir**2)) or 1.0
        tail = np.convolve(x.astype(np.float64), ir)[: len(x)]
        return x.astype(np.float64) * (1 - self.wet) + tail * self.wet

    def _bandpass(self, x: np.ndarray, sr: int) -> np.ndarray:
        """Zero-phase band limit with raised-cosine transition bands."""
        spec = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(len(x), 1 / sr)
        lo, hi = self.band
        mask = np.ones_like(freqs)
        # 100 Hz transitions either side, so we do not ring like a brickwall.
        w = 100.0
        rising = (freqs > lo - w) & (freqs < lo)
        mask[freqs <= lo - w] = 0.0
        mask[rising] = 0.5 * (1 - np.cos(np.pi * (freqs[rising] - (lo - w)) / w))
        falling = (freqs > hi) & (freqs < hi + w)
        mask[falling] = 0.5 * (1 + np.cos(np.pi * (freqs[falling] - hi) / w))
        mask[freqs >= hi + w] = 0.0
        return np.fft.irfft(spec * mask, n=len(x))

    def _agc(self, x: np.ndarray, sr: int) -> np.ndarray:
        """Compressor with fast attack and slow release.

        The release must be far longer than a coin pulse (hence the 200ms
        default). A symmetric follower would track each pulse individually and
        squash the very burst structure we are trying to measure — that is an
        artifact of the model, not something a handset actually does.
        """
        if self.agc_ratio <= 1:
            return x
        env = _envelope(x, sr, window_ms=2.0)
        level = _attack_release(env, sr, self.agc_attack_ms, self.agc_release_ms)
        ref = float(np.percentile(level, 95)) or 1.0
        over = np.maximum(level / ref, 1e-9)
        gain = np.where(over > 1.0, over ** (1.0 / self.agc_ratio - 1.0), 1.0)
        return x * gain

    def _noise(self, x: np.ndarray) -> np.ndarray:
        if self.noise_db is None:
            return x
        rng = np.random.default_rng(self.seed + 1)
        peak = float(np.max(np.abs(x))) or 1.0
        return x + rng.standard_normal(len(x)) * peak * 10.0 ** (self.noise_db / 20.0)


def _attack_release(
    env: np.ndarray, sr: int, attack_ms: float, release_ms: float
) -> np.ndarray:
    """One-pole follower with asymmetric time constants."""
    a_att = float(np.exp(-1.0 / max(1e-9, sr * attack_ms / 1000.0)))
    a_rel = float(np.exp(-1.0 / max(1e-9, sr * release_ms / 1000.0)))
    out = np.empty_like(env)
    prev = float(env[0]) if len(env) else 0.0
    for i, v in enumerate(env):
        a = a_att if v > prev else a_rel
        prev = a * prev + (1.0 - a) * float(v)
        out[i] = prev
    return out


def _envelope(x: np.ndarray, sr: int, window_ms: float = 4.0) -> np.ndarray:
    """Broadband envelope via the analytic signal.

    Deliberately *not* rectify-and-smooth: at an 8 kHz sample rate the second
    harmonic of a 2200 Hz coin tone lands at 4400 Hz, above Nyquist, so
    rectification aliases it back down as a spurious low-frequency beat. That
    beat reads as amplitude ripple and corrupts any burst counting built on it.
    """
    n_fft = len(x)
    if n_fft < 2:
        return np.abs(x)
    spec = np.fft.fft(x)
    h = np.zeros(n_fft)
    h[0] = 1.0
    if n_fft % 2 == 0:
        h[n_fft // 2] = 1.0
        h[1 : n_fft // 2] = 2.0
    else:
        h[1 : (n_fft + 1) // 2] = 2.0
    env = np.abs(np.fft.ifft(spec * h))
    n = max(1, int(sr * window_ms / 1000.0))
    return np.convolve(env, np.ones(n) / n, mode="same")


def _tone_envelope(
    x: np.ndarray, sr: int, freqs, window_ms: float = 8.0
) -> np.ndarray:
    """Envelope of the energy *at the coin frequencies only*.

    This is what a real coin detector measures — complex demodulation to
    baseband followed by a low-pass, i.e. a sliding Goertzel. Broadband energy
    from room noise or speech does not move it.
    """
    n = max(1, int(sr * window_ms / 1000.0))
    kernel = np.ones(n) / n
    t = np.arange(len(x)) / sr
    total = np.zeros(len(x), dtype=np.float64)
    for f in freqs:
        baseband = x * np.exp(-2j * np.pi * f * t)
        total += np.abs(np.convolve(baseband, kernel, mode="same"))
    return total / max(1, len(freqs))


# ---- burst integrity ---------------------------------------------------------


@dataclass
class BurstReport:
    """What a far-end detector would see after the path mangles the tone."""

    bursts: int
    expected: int
    gap_depth_db: float          # deepest-to-shallowest gap floor, vs burst peak
    mean_on_ms: float
    intact: bool = field(default=False)

    @property
    def verdict(self) -> str:
        if self.intact:
            return "ok"
        if self.bursts < self.expected:
            return f"gaps filled ({self.bursts}/{self.expected} pulses resolvable)"
        if self.bursts > self.expected:
            return f"pulse train fragmented ({self.bursts}/{self.expected})"
        return "on-time out of tolerance"


def burst_report(
    x: np.ndarray,
    sr: int,
    expected: int,
    freqs=None,
    gap_floor_db: float = -12.0,
    threshold: float = 0.5,
) -> BurstReport:
    """Count resolvable pulses and measure how far the inter-pulse gaps drop.

    ``freqs`` restricts the measurement to the coin frequencies, matching what a
    real detector sees; omit it to fall back on a broadband envelope.

    ``gap_floor_db`` is how far below the burst peak a gap must fall before a
    detector will read it as a gap rather than as amplitude ripple on one long
    tone. -12 dB is a forgiving stand-in for a real Goertzel-with-hysteresis.
    """
    env = _tone_envelope(x, sr, freqs) if freqs else _envelope(x, sr)
    peak = float(np.max(env)) or 1.0

    # Schmitt trigger, as any real tone detector uses: enter a burst at the
    # threshold but do not leave it until the envelope drops well below, so
    # amplitude ripple inside a burst cannot masquerade as a gap.
    hi, lo = peak * threshold, peak * threshold * 0.5
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(env):
        if start is None and v > hi:
            start = i
        elif start is not None and v < lo:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(env)))

    # Discard specks shorter than 8ms — ripple, not pulses.
    min_len = max(1, int(sr * 0.008))
    runs = [r for r in runs if r[1] - r[0] >= min_len]

    if len(runs) < 2:
        gap_db = 0.0
    else:
        floors = [float(np.min(env[a[1]:b[0]])) if b[0] > a[1] else peak
                  for a, b in zip(runs, runs[1:])]
        worst = max(floors) or 1e-12   # shallowest gap is the one that fails
        gap_db = 20.0 * np.log10(max(worst, 1e-12) / peak)

    mean_on = (float(np.mean([r[1] - r[0] for r in runs])) / sr * 1000.0
               if runs else 0.0)
    intact = len(runs) == expected and (expected < 2 or gap_db <= gap_floor_db)
    return BurstReport(bursts=len(runs), expected=expected, gap_depth_db=gap_db,
                       mean_on_ms=mean_on, intact=intact)


# ---- blind scheme discovery --------------------------------------------------


@dataclass
class Segment:
    """One tone-or-silence run recovered from audio, with no prior assumptions."""

    start_s: float
    dur_ms: float
    freqs: list[float]
    level_dbfs: float
    silent: bool

    def describe(self) -> str:
        if self.silent:
            return f"  {self.start_s:>7.3f}s  silence  {self.dur_ms:>6.1f}ms"
        f = "+".join(f"{x:.0f}" for x in self.freqs) or "?"
        return (f"  {self.start_s:>7.3f}s  {f:>12}Hz  {self.dur_ms:>6.1f}ms  "
                f"{self.level_dbfs:>6.1f} dBFS")


def scan_segments(
    x: np.ndarray,
    sr: int,
    silence_db: float = -45.0,
    min_ms: float = 12.0,
    peak_ratio: float = 0.25,
) -> list[Segment]:
    """Recover a coin scheme from audio without assuming its frequencies.

    Segments on broadband energy, then measures each tone run independently, so
    a scheme that has been changed out from under you still reads correctly:
    frequencies come from the spectrum, level from the envelope, duration from
    the run length. This is the detection half of "the tones changed".
    """
    env = _envelope(x, sr, window_ms=3.0)
    peak = float(np.max(env)) or 1.0
    gate = peak * 10.0 ** (silence_db / 20.0)
    loud = env > gate

    runs: list[tuple[int, int, bool]] = []
    start, cur = 0, bool(loud[0]) if len(loud) else False
    for i, v in enumerate(loud):
        if bool(v) != cur:
            runs.append((start, i, cur))
            start, cur = i, bool(v)
    runs.append((start, len(loud), cur))

    min_len = max(1, int(sr * min_ms / 1000.0))
    out: list[Segment] = []
    for a, b, is_tone in runs:
        if b - a < min_len:
            continue
        dur_ms = (b - a) / sr * 1000.0
        if not is_tone:
            out.append(Segment(a / sr, dur_ms, [], -np.inf, True))
            continue
        # Measure the steady middle, clear of the fades.
        pad = (b - a) // 6
        core = x[a + pad : b - pad] if (b - a) > 4 * pad > 0 else x[a:b]
        out.append(Segment(a / sr, dur_ms, _peak_freqs(core, sr, peak_ratio),
                           _level_dbfs(core, sr), False))
    return out


def _peak_freqs(x: np.ndarray, sr: int, peak_ratio: float) -> list[float]:
    """Dominant frequencies, interpolated past the FFT bin grid."""
    if len(x) < 8:
        return []
    n = 1 << (int(np.ceil(np.log2(len(x)))) + 3)   # zero-pad for resolution
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), n=n))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    top = float(np.max(spec)) or 1.0
    found: list[float] = []
    for i in range(1, len(spec) - 1):
        if spec[i] <= spec[i - 1] or spec[i] < spec[i + 1]:
            continue
        if spec[i] < top * peak_ratio:
            continue
        # Parabolic interpolation around the peak bin.
        a, b_, c = spec[i - 1], spec[i], spec[i + 1]
        denom = a - 2 * b_ + c
        off = 0.5 * (a - c) / denom if denom else 0.0
        f = float(freqs[i] + off * (freqs[1] - freqs[0]))
        if all(abs(f - g) > 40.0 for g in found):
            found.append(f)
    return sorted(found)[:4]


def _level_dbfs(x: np.ndarray, sr: int) -> float:
    """Combined *peak* level, matching the PhreakMe definition of ``level_dbfs``.

    Near-max rather than a mid percentile, and lightly smoothed: two summed
    tones beat at their difference frequency, so a wider window or a lower
    percentile averages across the beat and under-reads the level by several dB.
    Level is semantic in this scheme — nickel and dime differ by nothing else —
    so that error would silently turn one coin into another.
    """
    if not len(x):
        return -np.inf
    # Straight off the waveform: a smoothed envelope biases the reading high by
    # a few tenths of a dB, and tenths matter when level carries meaning. The
    # percentile rather than max() keeps a stray noise sample from inflating it.
    p = float(np.percentile(np.abs(x), 99.9))
    return 20.0 * np.log10(p) if p > 0 else -np.inf


def spec_from_segments(segs: list[Segment], round_ms: int = 5) -> list[list]:
    """Turn scanned segments into a PHREAKME_COINS-style pattern.

    Emits ``[freqs | None, seconds, level_dbfs]`` entries that can be dropped
    straight into a ``--coin-spec`` file, so a rediscovered scheme is playable
    without touching code.
    """
    out: list[list] = []
    for s in segs:
        dur = round(s.dur_ms / round_ms) * round_ms / 1000.0
        if s.silent:
            out.append([None, dur, 0.0])
        else:
            out.append([[round(f) for f in s.freqs], dur, round(s.level_dbfs, 1)])
    return out


def survivable(
    probe: CoinProbe, base: Config, path: AcousticPath
) -> BurstReport:
    """Render a probe, push it through the acoustic path, and grade it."""
    from .engine import ToneEngine as _TE

    pulses = _TE.US_REDBOX_COINS[probe.coin][0]
    clean = probe.render(base)
    heard = path.apply(clean, base.sample_rate)
    return burst_report(heard, base.sample_rate, expected=pulses, freqs=probe.freqs)
