"use strict";

// Client-side Bell System R1 MF synthesis via Web Audio API oscillators.
// No server required — works fully offline.

const MF_DIGITS = {
  "1": [700, 900],  "2": [700, 1100], "3": [900, 1100],
  "4": [700, 1300], "5": [900, 1300], "6": [1100, 1300],
  "7": [700, 1500], "8": [900, 1500], "9": [1100, 1500],
  "0": [1300, 1500],
};
const MF_SPECIAL = { KP: [1100, 1700], ST: [1500, 1700] };
const SEIZURE_FREQ = 2600;
const FADE_S = 0.005;

function validateDigits(digits) {
  for (const d of digits) {
    if (d === " " || d === "-") continue;
    if (!MF_DIGITS[d]) throw new Error(`"${d}" is not a valid MF digit`);
  }
}

function buildSchedule(digits, cfg) {
  const events = [];
  let t = 0;

  if (cfg.seize_duration > 0) {
    events.push({ freqs: [SEIZURE_FREQ], start: t, dur: cfg.seize_duration });
    t += cfg.seize_duration;
  }

  if (!cfg.seize_only) {
    t += cfg.wink_delay || 0;
    if (cfg.kp_duration > 0) {
      events.push({ freqs: MF_SPECIAL.KP, start: t, dur: cfg.kp_duration });
      t += cfg.kp_duration;
    }
    for (const d of digits) {
      if (d === " " || d === "-") continue;
      t += cfg.inter_digit_gap || 0;
      events.push({ freqs: MF_DIGITS[d], start: t, dur: cfg.digit_duration });
      t += cfg.digit_duration;
    }
    t += cfg.inter_digit_gap || 0;
    if (cfg.st_duration > 0) {
      events.push({ freqs: MF_SPECIAL.ST, start: t, dur: cfg.st_duration });
      t += cfg.st_duration;
    }
  }

  return { events, total: t };
}

function _scheduleEvent(ctx, dest, freqs, at, dur, amplitude) {
  if (dur <= 0) return;
  const fade = Math.min(FADE_S, dur / 4);
  const gain = ctx.createGain();
  gain.gain.setValueAtTime(0, at);
  gain.gain.linearRampToValueAtTime(amplitude / freqs.length, at + fade);
  gain.gain.setValueAtTime(amplitude / freqs.length, at + dur - fade);
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

// Play sequence immediately through a live AudioContext.
// Returns total duration in seconds.
function playSequenceLive(ctx, dest, digits, cfg) {
  const { events, total } = buildSchedule(digits, cfg);
  const base = ctx.currentTime + 0.05;
  for (const ev of events) {
    _scheduleEvent(ctx, dest, ev.freqs, base + ev.start, ev.dur, cfg.amplitude);
  }
  return total;
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

// ---- Coin tones ----------------------------------------------------------

// 1-slot ACTS red-box specs (Bell System, confirmed):
// All denominations: 1700 Hz + 2200 Hz dual tone.
// Denomination encoded by number and duration of bursts.
const COIN_FREQS_1SLOT = [1700, 2200];
const COIN_1SLOT = {
  nickel:  { pulses: 1, on: 0.066, off: 0.066 },
  dime:    { pulses: 2, on: 0.066, off: 0.066 },
  quarter: { pulses: 5, on: 0.033, off: 0.033 },
  dollar:  { pulses: 1, on: 0.650, off: 0     },
};

// 3-slot mechanical bell tones (synthesized approximation).
// Real phones use physical bells struck by coins — these are the operator-
// heard sounds, not electronic signals. Frequencies vary by phone model;
// values here are common approximations used in emulation.
const COIN_3SLOT = {
  nickel:  { freq: 1664, pulses: 1, bellDur: 0.35, gap: 0 },
  dime:    { freq: 1664, pulses: 2, bellDur: 0.35, gap: 0.2 },
  quarter: { freq: 800,  pulses: 1, bellDur: 0.70, gap: 0 },  // lower gong
};

// Schedule one exponentially-decaying bell strike (mimics physical ding).
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

// Play coin tone live. slot = "1slot" | "3slot".
// denomination = "nickel" | "dime" | "quarter" | "dollar" (dollar: 1-slot only).
// Returns total duration in seconds.
function playCoinLive(ctx, dest, denomination, slot, amplitude) {
  const base = ctx.currentTime + 0.05;
  amplitude = amplitude || 0.7;
  if (slot === "1slot") {
    const spec = COIN_1SLOT[denomination];
    if (!spec) throw new Error("Unknown denomination: " + denomination);
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      _scheduleEvent(ctx, dest, COIN_FREQS_1SLOT, base + t, spec.on, amplitude);
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

// Render coin tone to WAV bytes.
async function renderCoinToWav(denomination, slot, amplitude, sampleRate) {
  const sr = sampleRate || 8000;
  amplitude = amplitude || 0.7;
  let totalDur = 0;
  const scheduleItems = []; // collect what to render

  if (slot === "1slot") {
    const spec = COIN_1SLOT[denomination];
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      scheduleItems.push({ type: "tone", freqs: COIN_FREQS_1SLOT, at: t, dur: spec.on });
      t += spec.on + spec.off;
    }
    totalDur = t;
  } else {
    const spec = COIN_3SLOT[denomination];
    let t = 0;
    for (let i = 0; i < spec.pulses; i++) {
      scheduleItems.push({ type: "bell", freq: spec.freq, at: t, dur: spec.bellDur });
      t += spec.bellDur + spec.gap;
    }
    totalDur = t;
  }

  const nFrames = Math.ceil(totalDur * sr) + sr;
  const offline = new OfflineAudioContext(1, nFrames, sr);
  for (const item of scheduleItems) {
    if (item.type === "tone") {
      _scheduleEvent(offline, offline.destination, item.freqs, item.at, item.dur, amplitude);
    } else {
      _scheduleBell(offline, offline.destination, item.freq, item.at, item.dur, amplitude);
    }
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
