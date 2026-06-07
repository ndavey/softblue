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

const US_REDBOX_BURSTS = {
  "1": [[0.066, 0.100]],
  "2": [[0.066, 0.066], [0.066, 0.100]],
  "3": [[0.033, 0.033], [0.033, 0.033], [0.033, 0.033], [0.033, 0.033], [0.033, 0.100]],
  "4": [[0.650, 0.100]],
};
const US_REDBOX_FREQS_ACTS = [1700, 2200];
const US_REDBOX_FREQS_PHREAKME = [1700];

const UK_REDBOX = { "1": [1000, 0.200], "2": [1000, 0.350] };

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
  if (mode === "dtmf")      return DTMF_DIGITS[ch.toUpperCase()] != null;
  if (mode === "us_redbox") return US_REDBOX_BURSTS[ch] != null;
  if (mode === "uk_redbox") return UK_REDBOX[ch] != null;
  if (mode === "pulse_2600") return /^[0-9]$/.test(ch);
  if (mode === "green_box") return GREEN_BOX[ch] != null;
  return false;
}

function _modeLabel(mode) {
  return {mf_r1:"MF", c5:"C5", dtmf:"DTMF",
          us_redbox:"US red-box", uk_redbox:"UK red-box",
          pulse_2600:"2600-pulse", green_box:"green-box"}[mode] || mode;
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

  else if (mode === "dtmf") {
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      push(DTMF_DIGITS[ch.toUpperCase()], cfg.digit_duration);
    }
  }

  else if (mode === "us_redbox") {
    const freqs = cfg.coin_scheme === "phreakme"
        ? US_REDBOX_FREQS_PHREAKME : US_REDBOX_FREQS_ACTS;
    let first = true;
    for (const ch of digits) {
      if (ch === " " || ch === "-") continue;
      if (!first) gap(cfg.inter_digit_gap);
      first = false;
      for (const [on, off] of US_REDBOX_BURSTS[ch]) {
        push(freqs, on);
        gap(off);
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

function _scheduleEvent(ctx, dest, freqs, at, dur, amplitude) {
  dur = _safeDur(dur);
  amplitude = _safeDur(amplitude, 0.7);
  if (dur <= 0 || !isFinite(at)) return;
  const fade = Math.min(FADE_S, dur / 4);
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
    _scheduleEvent(ctx, dest, ev.freqs, base + ev.start, ev.dur, cfg.amplitude);
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
      _scheduleEvent(ctx, dest, ev.freqs,
                     startBase + t + ev.start, ev.dur, cfg.amplitude);
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
    _scheduleEvent(offline, offline.destination, ev.freqs, ev.start, ev.dur, cfg.amplitude);
  }
  const buffer = await offline.startRendering();
  return _encodeWav(buffer.getChannelData(0), sr);
}

// ---- Coin tones (1-slot ACTS / PhreakMe and 3-slot bell) ----------------

const COIN_1SLOT = {
  nickel:  { pulses: 1, on: 0.066, off: 0.066 },
  dime:    { pulses: 2, on: 0.066, off: 0.066 },
  quarter: { pulses: 5, on: 0.033, off: 0.033 },
  dollar:  { pulses: 1, on: 0.650, off: 0     },
};

// 3-slot mechanical bell tones (synthesized approximation).
const COIN_3SLOT = {
  nickel:  { freq: 1664, pulses: 1, bellDur: 0.35, gap: 0 },
  dime:    { freq: 1664, pulses: 2, bellDur: 0.35, gap: 0.2 },
  quarter: { freq: 800,  pulses: 1, bellDur: 0.70, gap: 0 },
};

function _scheduleBell(ctx, dest, freq, at, dur, amplitude) {
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

// Play coin tone live. ``scheme`` ("acts"|"phreakme") only applies to 1-slot.
function playCoinLive(ctx, dest, denomination, slot, amplitude, scheme) {
  const base = ctx.currentTime + 0.05;
  amplitude = amplitude || 0.7;
  if (slot === "1slot") {
    const spec = COIN_1SLOT[denomination];
    if (!spec) throw new Error("Unknown denomination: " + denomination);
    const freqs = scheme === "phreakme" ? US_REDBOX_FREQS_PHREAKME : US_REDBOX_FREQS_ACTS;
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      _scheduleEvent(ctx, dest, freqs, base + t, spec.on, amplitude);
      t += spec.on + spec.off;
    }
    return t;
  } else {
    const spec = COIN_3SLOT[denomination];
    if (!spec) throw new Error("Unknown denomination: " + denomination);
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      _scheduleBell(ctx, dest, spec.freq, base + t, spec.bellDur, amplitude);
      t += spec.bellDur + spec.gap;
    }
    return t;
  }
}

async function renderCoinToWav(denomination, slot, amplitude, sampleRate, scheme) {
  const sr = sampleRate || 8000;
  amplitude = amplitude || 0.7;
  const items = [];
  let total = 0;
  if (slot === "1slot") {
    const spec = COIN_1SLOT[denomination];
    const freqs = scheme === "phreakme" ? US_REDBOX_FREQS_PHREAKME : US_REDBOX_FREQS_ACTS;
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      items.push({ type: "tone", freqs, at: t, dur: spec.on });
      t += spec.on + spec.off;
    }
    total = t;
  } else {
    const spec = COIN_3SLOT[denomination];
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      items.push({ type: "bell", freq: spec.freq, at: t, dur: spec.bellDur });
      t += spec.bellDur + spec.gap;
    }
    total = t;
  }
  const nFrames = Math.ceil(total * sr) + sr;
  const offline = new OfflineAudioContext(1, nFrames, sr);
  for (const it of items) {
    if (it.type === "tone")
      _scheduleEvent(offline, offline.destination, it.freqs, it.at, it.dur, amplitude);
    else
      _scheduleBell(offline, offline.destination, it.freq, it.at, it.dur, amplitude);
  }
  const buffer = await offline.startRendering();
  return _encodeWav(buffer.getChannelData(0), sr);
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
