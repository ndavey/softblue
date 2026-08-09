"use strict";

// Client-side tone synthesis via Web Audio API. Mirrors the Python engine:
// MF/R1, C5, DTMF, US red-box (ACTS + PhreakMe), UK red-box, 2600 dial-pulse,
// plus 1-slot / 3-slot coin tones.

// ---- tone tables ---------------------------------------------------------

const MF_DIGITS = {
  "1": [700, 900],  "2": [700, 1100], "3": [900, 1100],
  "4": [700, 1300], "5": [900, 1300], "6": [1100, 1300],
  "7": [700, 1500], "8": [900, 1500], "9": [1100, 1500],
  "0": [1300, 1500],
};
const MF_SPECIAL = { KP: [1100, 1700], ST: [1500, 1700] };
// Coin/payphone KP and ST variants (used by PhreakMe period-accurate infrastructure)
const MF_SPECIAL_COIN = { KP: [1700, 2200], ST: [1500, 2200] };
const SEIZURE_FREQ = 2600;     // MF/R1
const C5_SF_FREQ   = 2400;     // CCITT #5

const DTMF_DIGITS = {
  "1": [1209, 697], "2": [1336, 697], "3": [1477, 697], "A": [1633, 697],
  "4": [1209, 770], "5": [1336, 770], "6": [1477, 770], "B": [1633, 770],
  "7": [1209, 852], "8": [1336, 852], "9": [1477, 852], "C": [1633, 852],
  "*": [1209, 941], "0": [1336, 941], "#": [1477, 941], "D": [1633, 941],
};

// [pulses, onSeconds, gapSeconds]; each coin is followed by COIN_TRAIL_S.
const US_REDBOX_COINS = {
  "1": [1, 0.066, 0.066],   // nickel
  "2": [2, 0.066, 0.066],   // dime
  "3": [5, 0.033, 0.033],   // quarter
  "4": [1, 0.650, 0.100],   // dollar — non-standard, real ACTS has no dollar tone
};
const COIN_TRAIL_S = 0.100;

const US_REDBOX_FREQS = {
  acts: [1700, 2200],   // Bell ACTS dual tone
  nortel: [2200],       // Canadian / Nortel single tone
  phreakme: [1700],     // single 1700 Hz
};

// PhreakMe's own coin scheme — NOT Bell ACTS. Mirrors PHREAKME_COINS in
// engine.py. Segments are [freqs|null, seconds, level_dBFS]; null = silence.
// Level is semantic: nickel and dime are the same 1700 Hz tone at -6 vs -3 dBFS,
// so this path must ignore the global amplitude control.
const PHREAKME_COINS = {
  n: [[[1700], 0.060, -6.0], [null, 0.060, 0]],
  d: [[[1700], 0.060, -3.0], [null, 0.060, 0]],
  q: [[[1700], 0.060, -3.0], [null, 0.060, 0], [[2200], 0.060, -3.0], [null, 0.060, 0]],
  $: [[[1700, 2200], 0.060, -3.0], [null, 0.060, 0]],
  c: [[[1700], 0.060, -3.0], [null, 0.060, 0], [[1700], 0.060, -3.0], [null, 0.060, 0],
      [[1700], 0.060, -3.0], [null, 0.060, 0]],
  r: [[[1700], 0.060, -3.0], [null, 0.015, 0], [[1700], 0.060, -3.0], [null, 0.015, 0],
      [[1700], 0.060, -3.0], [null, 0.015, 0], [[1700], 0.060, -3.0], [null, 0.015, 0],
      [[1700], 0.060, -3.0], [null, 0.015, 0], [[1700], 0.060, -3.0], [null, 0.015, 0]],
};
const PHREAKME_FADE_S = 0.002;   // non-optional per the PhreakMe tone spec §3.3

// ---- red-box scheme search (mirrors redbox.py) ---------------------------
//
// The whole coin table above is generated from ONE frequency pair: 1700 carries
// nickel/dime/collect/return, 2200 is the quarter's second symbol and the
// dollar's partner. Everything else is structure. So when the organisers change
// the frequencies, only that pair moves — which is why this page can chase a
// changed scheme without a code edit, and why the search space is just the
// ordered pairs over the frequencies their own detector can measure.
//
// Kept client-side on purpose: the browser is what gets held against the
// handset, and it has to keep working when the venue wifi does not.

const MF_ALPHABET = [700, 900, 1100, 1300, 1500, 1700, 2200];
const KNOWN_A = 1700, KNOWN_B = 2200;
const CONTROL = {
  duration: 0.060, gap: 0.060, return_gap: 0.015,
  nickel_dbfs: -6.0, dime_dbfs: -3.0, level_dbfs: -3.0,
};

// Build the full six-symbol coin table from one scheme. Same shape as
// RedboxScheme.coin_spec(): { symbol: [[freqs|null, seconds, dBFS], …] }.
function redboxCoinSpec(scheme) {
  const s = Object.assign({ freq_a: KNOWN_A, freq_b: KNOWN_B }, CONTROL, scheme || {});
  const a = [s.freq_a], b = [s.freq_b];
  const d = s.duration, g = s.gap, L = s.level_dbfs;
  const rep = (segs, n) => Array.from({ length: n }, () => segs).flat();
  return {
    n: [[a, d, s.nickel_dbfs], [null, g, 0]],
    d: [[a, d, s.dime_dbfs], [null, g, 0]],
    q: [[a, d, L], [null, g, 0], [b, d, L], [null, g, 0]],
    $: [[[s.freq_a, s.freq_b], d, L], [null, g, 0]],
    c: rep([[a, d, L], [null, g, 0]], 3),
    r: rep([[a, d, L], [null, s.return_gap, 0]], 6),
  };
}

function redboxIsControl(s) {
  return s.freq_a === KNOWN_A && s.freq_b === KNOWN_B
      && Object.keys(CONTROL).every(k =>
           Math.abs((s[k] != null ? s[k] : CONTROL[k]) - CONTROL[k]) < 1e-9);
}

/**
 * Stable identity for a scheme, mirroring RedboxScheme.name in redbox.py.
 * The pair alone is not an identity — candidates that keep 1700->2200 and move
 * the segment length or the nickel/dime split are distinct experiments, and
 * they are the sweep's own log keys.
 */
function redboxName(s) {
  if (s.label) return s.label;
  const marks = [];
  const d = (k) => (s[k] != null ? s[k] : CONTROL[k]);
  if (Math.abs(d("duration") - CONTROL.duration) > 1e-9)
    marks.push(`${Math.round(d("duration") * 1000)}ms`);
  if (Math.abs(d("gap") - CONTROL.gap) > 1e-9)
    marks.push(`gap${Math.round(d("gap") * 1000)}ms`);
  if (Math.abs(d("return_gap") - CONTROL.return_gap) > 1e-9)
    marks.push(`rgap${Math.round(d("return_gap") * 1000)}ms`);
  if (Math.abs(d("nickel_dbfs") - CONTROL.nickel_dbfs) > 1e-9
      || Math.abs(d("dime_dbfs") - CONTROL.dime_dbfs) > 1e-9)
    marks.push(`n${d("nickel_dbfs").toFixed(0)}/d${d("dime_dbfs").toFixed(0)}`);
  if (Math.abs(d("level_dbfs") - CONTROL.level_dbfs) > 1e-9)
    marks.push(`L${d("level_dbfs").toFixed(0)}`);
  return `${s.freq_a}->${s.freq_b}` + (marks.length ? " " + marks.join(" ") : "");
}

function redboxDescribe(s) {
  return `${s.freq_a}->${s.freq_b}Hz  ${Math.round((s.duration ?? 0.06) * 1000)}ms  `
       + `nickel ${(s.nickel_dbfs ?? -6).toFixed(0)} / `
       + `dime ${(s.dime_dbfs ?? -3).toFixed(0)} dBFS`
       + (redboxIsControl(s) ? "   [control: last year's scheme]" : "");
}

// Offline fallback ordering, mirroring redbox._priority: control, its reversal,
// pairs keeping an ACTS frequency, then the coin ST pair, then the rest by
// separation (close pairs are hardest for a Goertzel to tell apart).
function redboxCandidates() {
  const acts = new Set([KNOWN_A, KNOWN_B]);
  const tier = (a, b) =>
      (a === KNOWN_A && b === KNOWN_B) ? 0
    : (a === KNOWN_B && b === KNOWN_A) ? 1
    : (acts.has(a) && acts.has(b)) ? 2
    : (acts.has(a) || acts.has(b)) ? 3
    : (a === 1500 || b === 1500 || a === 2200 || b === 2200) ? 4 : 5;
  const out = [];
  for (const a of MF_ALPHABET) for (const b of MF_ALPHABET) {
    if (a !== b) out.push(Object.assign({ freq_a: a, freq_b: b }, CONTROL));
  }
  out.sort((p, q) =>
    tier(p.freq_a, p.freq_b) - tier(q.freq_a, q.freq_b)
    || Math.abs(q.freq_a - q.freq_b) - Math.abs(p.freq_a - p.freq_b)
    || p.freq_a - q.freq_a || p.freq_b - q.freq_b);
  return out.map((s, i) => Object.assign(s, {
    index: i + 1, label: redboxName(s),
    describe: redboxDescribe(s), is_control: redboxIsControl(s),
  }));
}

// ---- blind scheme recovery (mirrors sweep.py scan_segments) --------------
//
// Predicting the new pair is guesswork; *recording* it is not. If you can
// capture the real thing — a handed-out tone generator, or the challenge
// playing its own collect/return — one clean capture collapses the whole
// candidate list to a single answer. That is worth more than every ranked
// guess above, so it runs in the page rather than only in the CLI.
//
// Goertzel over the seven detectable frequencies rather than a full FFT: the
// answer can only be one of those, so there is nothing a general spectrum
// would tell us that this does not, and it is a tenth of the code.

function _goertzelAmp(x, from, to, sr, f) {
  const w = 2 * Math.cos(2 * Math.PI * f / sr);
  let s1 = 0, s2 = 0;
  for (let i = from; i < to; i++) { const s0 = x[i] + w * s1 - s2; s2 = s1; s1 = s0; }
  const n = to - from;
  if (n <= 0) return 0;
  // For a sine of amplitude A the magnitude is about A*n/2.
  return Math.sqrt(Math.max(0, s1 * s1 + s2 * s2 - w * s1 * s2)) * 2 / n;
}

/**
 * Sliding-RMS envelope, CENTRED on each sample. Cheap, and immune to the phase
 * of the carrier.
 *
 * Centred rather than causal: a look-back window reports every edge late, which
 * shifts each burst and reads its length long. Duration is semantic here — it
 * is one of the axes the scheme can move on — so a systematic few-ms error
 * would be adopted as a real finding.
 */
function _rmsEnvelope(x, sr, windowMs = 4) {
  const n = Math.max(1, Math.round(sr * windowMs / 1000));
  const half = n >> 1;
  const out = new Float32Array(x.length);
  let acc = 0;
  for (let i = 0; i < x.length + half; i++) {
    if (i < x.length) acc += x[i] * x[i];
    if (i >= n) acc -= x[i - n] * x[i - n];
    if (i < half) continue;
    // Divide by the samples actually inside the window, not by n. Where the
    // window hangs off either end of the buffer those two differ, and the
    // envelope reads low — which delays the opening gate crossing by half a
    // window and measures a capture that starts on a tone ~2 ms short.
    const lo = Math.max(0, i - n + 1);
    const hi = Math.min(x.length, i + 1);
    out[i - half] = Math.sqrt(acc / Math.max(1, hi - lo));
  }
  return out;
}

function _maxOf(a) {
  let m = 0;
  for (let i = 0; i < a.length; i++) if (a[i] > m) m = a[i];
  return m;
}

/**
 * Recover tone/silence runs from a recording, with no assumptions about which
 * frequencies are in it. Levels are reported RELATIVE to the loudest tone in
 * the capture — an acoustic path has arbitrary gain, so an absolute dBFS
 * reading would be meaningless, while the nickel/dime *ratio* still survives.
 */
function scanCoinSegments(x, sr, opts) {
  const { silenceDb = -30, minMs = 25 } = opts || {};
  const env = _rmsEnvelope(x, sr, 4);
  const peak = _maxOf(env);
  if (!peak) return [];

  // The gate has to clear the room, not just sit a fixed distance below the
  // peak. On a quiet capture the noise floor rises above a peak-relative gate,
  // the gate never closes, and every burst merges into one long run — which
  // reads a quarter as a simultaneous '$' at half a second per segment.
  // Confidently wrong, so: put the gate above the measured floor as well.
  const sorted = [];
  for (let i = 0; i < env.length; i += 32) sorted.push(env[i]);
  sorted.sort((a, b) => a - b);
  const floor = sorted[Math.floor(sorted.length * 0.1)] || 0;
  const gate = Math.max(peak * Math.pow(10, silenceDb / 20), floor * 2.5);
  // Nothing stands far enough out of the noise to segment reliably. Saying so
  // beats returning a scheme assembled from room tone.
  if (gate > peak * 0.7) return [];
  const minLen = Math.max(1, Math.round(sr * minMs / 1000));

  const runs = [];
  let start = 0, cur = env[0] > gate;
  for (let i = 1; i < env.length; i++) {
    const loud = env[i] > gate;
    if (loud !== cur) { runs.push([start, i, cur]); start = i; cur = loud; }
  }
  runs.push([start, env.length, cur]);

  // Any smoothing widens a run by half its window at each edge, so take that
  // back before reporting a length — but only for edges that are real. A run
  // clamped at the start or end of the buffer (a capture that opens mid-tone)
  // has one true edge and one artificial one, and charging it for both reads
  // the burst 2 ms short — enough to turn a 66 ms segment into a 65 ms one.
  const halfSmearMs = (Math.max(1, Math.round(sr * 4 / 1000)) / sr) * 1000 / 2;

  const segs = [];
  for (const [a, b, isTone] of runs) {
    if (b - a < minLen) continue;
    const edges = (a > 0 ? 1 : 0) + (b < env.length ? 1 : 0);
    const corr = halfSmearMs * edges * (isTone ? 1 : -1);
    const durMs = Math.max(0, (b - a) / sr * 1000 - corr);
    if (!isTone) { segs.push({ startS: a / sr, durMs, freqs: [], silent: true }); continue; }
    // Measure the steady middle, clear of the fades.
    const pad = Math.floor((b - a) / 6);
    const lo = a + pad, hi = b - pad;
    const amps = MF_ALPHABET.map(f => [f, _goertzelAmp(x, lo, hi, sr, f)]);
    amps.sort((p, q) => q[1] - p[1]);
    const top = amps[0][1] || 1e-12;
    // Tonality gate. A real tone stands ~90 dB above the other bins; broadband
    // noise sits at roughly the same level in all seven. Without this, a burst
    // of room noise reads as a confident single-tone "nickel" and gets adopted
    // as a scheme.
    const median = amps[Math.floor(amps.length / 2)][1] || 1e-12;
    if (top < median * 4) {
      segs.push({ startS: a / sr, durMs, freqs: [], silent: true, noise: true });
      continue;
    }
    // Keep the strongest, plus any within 6 dB of it — that is how a
    // simultaneous pair like '$' presents, and dropping the second one would
    // silently turn a dollar into a nickel.
    const freqs = amps.filter(p => p[1] >= top * 0.5).map(p => p[0]).sort((m, n) => m - n);
    segs.push({ startS: a / sr, durMs, freqs, amp: top, silent: false });
  }
  const loudest = Math.max(...segs.filter(s => !s.silent).map(s => s.amp), 1e-12);
  for (const s of segs) {
    if (!s.silent) s.relDb = 20 * Math.log10(s.amp / loudest);
  }
  return segs;
}

/**
 * Read a scheme out of scanned segments. Returns null when the capture does
 * not look like one of the six coin patterns — better to say "I don't know"
 * than to hand back a confident pair drawn from a cough.
 */
function inferCoinScheme(segs) {
  const tones = segs.filter(s => !s.silent && s.freqs.length);
  if (!tones.length) return null;
  // Every coin segment in this grammar is one short burst. A run far outside
  // that range means the segmentation failed, not that the organisers invented
  // a half-second coin — refuse rather than report a pattern read off it.
  if (tones.some(s => s.durMs < 20 || s.durMs > 250)) return null;
  const durMs = tones.reduce((a, s) => a + s.durMs, 0) / tones.length;
  // 1 ms, not the 5 ms grid spec_from_segments uses: measurement is now exact
  // enough to justify it, and a 5 ms grid would snap the ACTS-canonical 66 ms
  // to 65 — turning the timing-axis candidate into a near miss of itself.
  const duration = Math.round(durMs) / 1000;
  const uniq = [...new Set(tones.flatMap(s => s.freqs))];

  // '$' — both tones at once, in a single burst.
  if (tones.length === 1 && tones[0].freqs.length === 2) {
    return { freq_a: tones[0].freqs[0], freq_b: tones[0].freqs[1],
             duration, pattern: "dollar (A+B simultaneous)", confident: true };
  }
  // Quarter — two bursts, different frequencies, order is the payload.
  if (tones.length === 2 && tones[0].freqs[0] !== tones[1].freqs[0]) {
    return { freq_a: tones[0].freqs[0], freq_b: tones[1].freqs[0],
             duration, pattern: "quarter (A then B)", confident: true };
  }
  // Collect (3x A) / return (6x A) — loud, distinctive, and they pin A only.
  if (uniq.length === 1 && tones.length >= 3) {
    const what = tones.length === 6 ? "return (6x A)"
               : tones.length === 3 ? "collect (3x A)" : `${tones.length}x A`;
    return { freq_a: uniq[0], freq_b: null, duration, pattern: what,
             confident: tones.length === 3 || tones.length === 6 };
  }
  // A single burst pins the workhorse but says nothing about the marker.
  if (tones.length === 1) {
    return { freq_a: tones[0].freqs[0], freq_b: null, duration,
             pattern: "single tone (nickel or dime — A only)", confident: false };
  }
  return { freq_a: tones[0].freqs[0], freq_b: uniq.find(f => f !== tones[0].freqs[0]) ?? null,
           duration, pattern: `${tones.length} bursts — unrecognised`, confident: false };
}

const UK_REDBOX = { "1": [1000, 0.200], "2": [1000, 0.350] };

// 3-slot payphone gong/bell tones — [freq, pulses, bellDur, dingGap].
const BELL_3SLOT = {
  "1": [1664, 1, 0.35, 0.0],   // nickel — one ding
  "2": [1664, 2, 0.35, 0.20],  // dime — two dings
  "3": [800, 1, 0.70, 0.0],    // quarter — gong
};

// Green box: operator/TSPS coin-control tones — [freq_pair, on_seconds].
const GREEN_BOX = {
  "c": [[700, 1100], 1.0],    // coin collect
  "r": [[1100, 1700], 1.0],   // coin return
  "b": [[700, 1700], 2.0],    // ringback
};
const GREEN_WINK_FREQ = 2600;      // 2600 Hz operator-release signal
const GREEN_WINK_MF8 = [900, 1500]; // MF "8" wink alternative
const GREEN_WINK_ON1_S = 0.090;    // first burst (both wink styles)
const GREEN_WINK_GAP_S = 0.060;    // inter-burst silence
const GREEN_WINK_ON2_S = 0.900;    // second 2600 Hz burst (2600 style only)

const PULSE_BREAK_S = 0.060;
const PULSE_MAKE_S = 0.040;

const FADE_S = 0.005;
const CLEAR_S = 0.100;   // mf inline "x" clear duration

// ---- validation ---------------------------------------------------------

function validateDigits(digits, mode) {
  mode = mode || "mf_r1";
  if (mode === "mf_r1" || mode === "c5" || mode === "green_box")
    digits = (digits || "").toLowerCase();
  for (const d of digits) {
    if (d === " " || d === "-") continue;
    if (!_isValid(d, mode)) {
      throw new Error(`"${d}" is not a valid ${_modeLabel(mode)} digit`);
    }
  }
}

function _isValid(ch, mode) {
  if (mode === "mf_r1" || mode === "c5") {
    return MF_DIGITS[ch] != null || "kszx.".indexOf(ch) >= 0;
  }
  if (mode === "dtmf" || mode === "autovon") return DTMF_DIGITS[ch.toUpperCase()] != null;
  if (mode === "us_redbox") return US_REDBOX_COINS[ch] != null;
  if (mode === "phreakme_coin") return PHREAKME_COINS[ch] != null;
  if (mode === "uk_redbox") return UK_REDBOX[ch] != null;
  if (mode === "pulse_2600") return /^[0-9]$/.test(ch);
  if (mode === "bell_3slot") return BELL_3SLOT[ch] != null;
  if (mode === "green_box") return GREEN_BOX[ch] != null;
  return false;
}

function _modeLabel(mode) {
  return {mf_r1:"MF", c5:"C5", dtmf:"DTMF",
          us_redbox:"US red-box", phreakme_coin:"PhreakMe coin",
          uk_redbox:"UK red-box",
          pulse_2600:"2600-pulse", bell_3slot:"3-slot bell",
          green_box:"green-box", autovon:"AUTOVON"}[mode] || mode;
}

// ---- schedule building --------------------------------------------------

function _hasInline(digits) {
  for (const ch of digits) if ("kszx.".indexOf(ch) >= 0) return true;
  return false;
}

function buildSchedule(digits, cfg) {
  const mode = cfg.mode || "mf_r1";
  const events = [];
  let t = 0;
  // _safeDur guards every cfg duration so a cleared input can't NaN-poison t.
  const push = (freqs, dur) => {
    dur = _safeDur(dur);
    if (dur > 0) { events.push({ freqs, start: t, dur }); t += dur; }
  };
  // Bell events carry an exponential-decay envelope (see _scheduleBell).
  const pushBell = (freq, dur) => {
    dur = _safeDur(dur);
    if (dur > 0) { events.push({ freqs: [freq], start: t, dur, bell: true }); t += dur; }
  };
  const gap = (s) => { const n = _safeDur(s); t += n; };

  if (mode === "mf_r1" || mode === "c5") {
    digits = (digits || "").toLowerCase();
    const defaultSf = mode === "c5" ? C5_SF_FREQ : SEIZURE_FREQ;
    const sf = (cfg.seize_freq && isFinite(cfg.seize_freq) && cfg.seize_freq > 0)
               ? cfg.seize_freq : defaultSf;
    // mf_variant: "coin" uses 1700+2200 KP / 1500+2200 ST (PhreakMe payphone variant)
    const special = cfg.mf_variant === "coin" ? MF_SPECIAL_COIN : MF_SPECIAL;
    if (cfg.seize_only) {
      if (cfg.seize_duration > 0) push([sf], cfg.seize_duration);
      return { events, total: t };
    }
    // `no_wrap` forces literal mode even without inline control chars. Used by
    // the per-keypress preview path so a plain digit doesn't drag KP+ST along.
    if (_hasInline(digits) || cfg.no_wrap) {
      let first = true;
      for (const ch of digits) {
        if (ch === " " || ch === "-") continue;
        if (!first) gap(cfg.inter_digit_gap);
        first = false;
        if (MF_DIGITS[ch])       push(MF_DIGITS[ch], cfg.digit_duration);
        else if (ch === "k")     push(special.KP, cfg.kp_duration);
        else if (ch === "s")     push(special.ST, cfg.st_duration);
        else if (ch === "z")     push([sf], cfg.seize_duration);
        else if (ch === "x")     push([sf], CLEAR_S);
        else if (ch === ".")     push([sf], cfg.digit_duration);
      }
    } else {
      if (cfg.seize_duration > 0) push([sf], cfg.seize_duration);
      gap(cfg.wink_delay);
      if (cfg.kp_duration > 0) push(special.KP, cfg.kp_duration);
      for (const ch of digits) {
        if (ch === " " || ch === "-") continue;
        gap(cfg.inter_digit_gap);
        push(MF_DIGITS[ch], cfg.digit_duration);
      }
      gap(cfg.inter_digit_gap);
      if (cfg.st_duration > 0) push(special.ST, cfg.st_duration);
    }
  }

  else if (mode === "dtmf" || mode === "autovon") {
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      push(DTMF_DIGITS[ch.toUpperCase()], cfg.digit_duration);
    }
  }

  else if (mode === "us_redbox") {
    const freqs = (cfg.coin_freqs && cfg.coin_freqs.length)
        ? cfg.coin_freqs
        : (US_REDBOX_FREQS[cfg.coin_scheme] || US_REDBOX_FREQS.acts);
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      const [pulses, defOn, defGap] = US_REDBOX_COINS[ch];
      const on = cfg.coin_on != null ? cfg.coin_on : defOn;
      const gapS = cfg.coin_gap != null ? cfg.coin_gap : defGap;
      for (let i = 0; i < pulses; i++) {
        push(freqs, on);
        gap(i < pulses - 1 ? gapS : COIN_TRAIL_S);
      }
    }
  }

  else if (mode === "phreakme_coin") {
    const table = (cfg.coin_spec && Object.keys(cfg.coin_spec).length)
        ? cfg.coin_spec : PHREAKME_COINS;
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!table[ch]) continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      for (const [freqs, dur, dbfs] of table[ch]) {
        if (!freqs) { gap(dur); continue; }
        const d = _safeDur(dur);
        if (d <= 0) continue;
        events.push({ freqs, start: t, dur: d,
                      amp: Math.pow(10, dbfs / 20), fade: PHREAKME_FADE_S });
        t += d;
      }
    }
  }

  else if (mode === "uk_redbox") {
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      const [freq, dur] = UK_REDBOX[ch];
      push([freq], dur);
    }
  }

  else if (mode === "pulse_2600") {
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      const n = parseInt(ch, 10);
      const pulses = n === 0 ? 10 : n;
      for (let i = 0; i < pulses; i++) {
        gap(PULSE_BREAK_S);
        push([SEIZURE_FREQ], PULSE_MAKE_S);
      }
    }
  }

  else if (mode === "bell_3slot") {
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      const spec = BELL_3SLOT[ch];
      if (!spec) continue;
      const [freq, pulses, bellDur, dingGap] = spec;
      for (let i = 0; i < pulses; i++) {
        if (i) gap(dingGap);
        pushBell(freq, bellDur);
      }
    }
  }

  else if (mode === "green_box") {
    digits = (digits || "").toLowerCase();
    const wink = cfg.green_wink === "mf8" ? "mf8" : "2600";
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      // Operator release wink.
      if (wink === "2600") {
        push([GREEN_WINK_FREQ], GREEN_WINK_ON1_S);
        gap(GREEN_WINK_GAP_S);
        push([GREEN_WINK_FREQ], GREEN_WINK_ON2_S);
      } else {
        push(GREEN_WINK_MF8, GREEN_WINK_ON1_S);
        gap(GREEN_WINK_GAP_S);
      }
      // Control tone.
      const spec = GREEN_BOX[ch];
      if (spec) push(spec[0], spec[1]);
    }
  }

  return { events, total: t };
}

// Clamp a config duration to a safe finite value so a cleared/NaN input
// field never propagates NaN into the Web Audio time or gain arguments.
function _safeDur(v, fallback) {
  const n = parseFloat(v);
  return (isFinite(n) && n >= 0) ? n : (fallback || 0);
}

// Events may carry their own amplitude (PhreakMe coins encode value as
// level, so the global amplitude control must not rescale them).
function _evAmp(ev, cfg) { return ev.amp != null ? ev.amp : cfg.amplitude; }

function _scheduleEvent(ctx, dest, freqs, at, dur, amplitude, fadeOverride) {
  dur = _safeDur(dur);
  amplitude = _safeDur(amplitude, 0.7);
  if (dur <= 0 || !isFinite(at)) return;
  // 5% of the burst per edge. dur/4 would put a 33ms coin pulse half inside its
  // own ramps, leaving too little at level for a detector to accept it.
  const fade = (fadeOverride != null)
      ? Math.min(fadeOverride, dur / 2)
      : Math.min(FADE_S, dur / 20);
  const gain = ctx.createGain();
  const vol = amplitude / Math.max(1, freqs.length);
  gain.gain.setValueAtTime(0, at);
  gain.gain.linearRampToValueAtTime(vol, at + fade);
  gain.gain.setValueAtTime(vol, at + dur - fade);
  gain.gain.linearRampToValueAtTime(0, at + dur);
  gain.connect(dest);

  for (const freq of freqs) {
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = freq;
    osc.connect(gain);
    osc.start(at);
    osc.stop(at + dur);
  }
}

// Play sequence immediately through a live AudioContext. Returns duration (s).
function playSequenceLive(ctx, dest, digits, cfg) {
  const { events, total } = buildSchedule(digits, cfg);
  const base = ctx.currentTime + 0.05;
  for (const ev of events) {
    if (ev.bell) _scheduleBell(ctx, dest, ev.freqs[0], base + ev.start, ev.dur, _evAmp(ev, cfg), ev.fade);
    else _scheduleEvent(ctx, dest, ev.freqs, base + ev.start, ev.dur, _evAmp(ev, cfg), ev.fade);
  }
  return total;
}

// Resolve a macro step into a (digits, cfg) pair. ``baseCfg`` is the page
// config; ``presetLookup`` is a name->preset map.
function _resolveStep(step, baseCfg, presetLookup) {
  if (step.preset) {
    const p = (presetLookup || {})[step.preset];
    if (!p) throw new Error(`preset "${step.preset}" not found`);
    return { digits: p.digits || "", cfg: Object.assign({}, baseCfg, p.config || {}) };
  }
  const cfg = Object.assign({}, baseCfg, step.config || {});
  if (step.mode) cfg.mode = step.mode;
  return { digits: step.digits || "", cfg };
}

// Play a macro (ordered steps with optional delay_after between them).
// Returns total duration in seconds.
function playMacroLive(ctx, dest, steps, baseCfg, presetLookup) {
  const startBase = ctx.currentTime + 0.05;
  let t = 0;
  for (const step of steps) {
    const { digits, cfg } = _resolveStep(step, baseCfg, presetLookup);
    validateDigits(digits, cfg.mode);
    const { events, total } = buildSchedule(digits, cfg);
    for (const ev of events) {
      if (ev.bell)
        _scheduleBell(ctx, dest, ev.freqs[0],
                      startBase + t + ev.start, ev.dur, _evAmp(ev, cfg), ev.fade);
      else
        _scheduleEvent(ctx, dest, ev.freqs,
                       startBase + t + ev.start, ev.dur, _evAmp(ev, cfg), ev.fade);
    }
    t += total + (step.delay_after || 0);
  }
  return t;
}

// Render sequence to a WAV ArrayBuffer (client-side, no server).
async function renderToWav(digits, cfg) {
  const sr = cfg.sample_rate || 8000;
  const { events, total } = buildSchedule(digits, cfg);
  const nFrames = Math.ceil(total * sr) + sr;
  const offline = new OfflineAudioContext(1, nFrames, sr);
  for (const ev of events) {
    if (ev.bell)
      _scheduleBell(offline, offline.destination, ev.freqs[0], ev.start, ev.dur, _evAmp(ev, cfg), ev.fade);
    else
      _scheduleEvent(offline, offline.destination, ev.freqs, ev.start, ev.dur, _evAmp(ev, cfg), ev.fade);
  }
  const buffer = await offline.startRendering();
  return _encodeWav(buffer.getChannelData(0), sr);
}

// ---- struck-bell envelope (used by the bell_3slot mode) -----------------

function _scheduleBell(ctx, dest, freq, at, dur, amplitude) {
  dur = _safeDur(dur);
  amplitude = _safeDur(amplitude, 0.7);
  if (dur <= 0 || !isFinite(at) || amplitude <= 0) return;
  const gain = ctx.createGain();
  gain.gain.setValueAtTime(amplitude, at + 0.002);
  gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  gain.connect(dest);
  const osc = ctx.createOscillator();
  osc.type = "sine";
  osc.frequency.value = freq;
  osc.connect(gain);
  osc.start(at);
  osc.stop(at + dur);
}

function _encodeWav(pcm, sr) {
  const int16 = new Int16Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, Math.round(pcm[i] * 32767)));
  }
  const dataBytes = int16.length * 2;
  const buf = new ArrayBuffer(44 + dataBytes);
  const v = new DataView(buf);
  const w = (off, s) => [...s].forEach((c, i) => v.setUint8(off + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + dataBytes, true);
  w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, dataBytes, true);
  new Int16Array(buf, 44).set(int16);
  return buf;
}
