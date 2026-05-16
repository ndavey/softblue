"use strict";

const $ = (id) => document.getElementById(id);
const TIMING = ["seize_duration", "wink_delay", "digit_duration",
                "inter_digit_gap", "amplitude", "sample_rate"];
const PRESETS_KEY = "softblue-presets";

let liveDebounce;
const LIVE_MS = 300;

function readConfig() {
  const c = {};
  for (const k of TIMING) c[k] = parseFloat($(k).value);
  c.seize_only = $("seize_only").checked;
  return c;
}

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = "badge" + (cls ? " " + cls : "");
}

function showError(msg) { $("error").textContent = msg || ""; }

// ---- audio graph ---------------------------------------------------------
let audioCtx, analyser;

function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.connect(audioCtx.destination);
    drawSpectrum();
  }
}

// Call synchronously inside a user-gesture handler (before any await).
function kickAudio() {
  ensureAudio();
  audioCtx.resume();
}

// ---- playback (all client-side, no server needed) ------------------------

function playLocal() {
  showError("");
  kickAudio();
  try {
    validateDigits($("digits").value);
    const dur = playSequenceLive(audioCtx, analyser, $("digits").value, readConfig());
    animateTimeline(dur);
  } catch (e) {
    showError(e.message);
  }
}

function playDigitTone(digit) {
  const cfg = Object.assign(readConfig(), {
    seize_duration: 0, wink_delay: 0,
    kp_duration: 0.05, inter_digit_gap: 0.02,
    digit_duration: 0.1, st_duration: 0.05,
    seize_only: false,
  });
  playSequenceLive(audioCtx, analyser, digit, cfg);
}

function playSeizeTone() {
  const cfg = Object.assign(readConfig(), { seize_only: true });
  playSequenceLive(audioCtx, analyser, "", cfg);
}

function playLiveDebounced() {
  if (!$("liveMode").checked) return;
  kickAudio();
  clearTimeout(liveDebounce);
  liveDebounce = setTimeout(() => playLocal(), LIVE_MS);
}

// ---- WAV download (client-side via OfflineAudioContext) ------------------

async function download() {
  showError("");
  try {
    validateDigits($("digits").value);
    const buf = await renderToWav($("digits").value, readConfig());
    const blob = new Blob([buf], { type: "audio/wav" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = ($("digits").value || "seize") + ".wav";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { showError(e.message); }
}

// ---- server-side play (optional, requires server) ------------------------

async function serverPlay() {
  showError("");
  const res = await fetch("/api/play", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digits: $("digits").value, config: readConfig() }),
  }).catch(() => null);
  if (!res || !res.ok) {
    const e = res ? await res.json().catch(() => ({})) : {};
    showError(e.detail || "server not available");
  }
}

// ---- timeline ------------------------------------------------------------
const tl = $("timeline");
function animateTimeline(duration) {
  const ctx = tl.getContext("2d");
  tl.width = tl.clientWidth;
  const start = performance.now();
  (function frame(now) {
    const p = Math.min(1, (now - start) / 1000 / duration);
    ctx.clearRect(0, 0, tl.width, tl.height);
    ctx.fillStyle = "#1f2a52";
    ctx.fillRect(0, 24, tl.width, 12);
    ctx.fillStyle = "#00d4ff";
    ctx.fillRect(0, 24, tl.width * p, 12);
    ctx.fillStyle = "#8892b0";
    ctx.font = "11px monospace";
    ctx.fillText(`${(p * duration).toFixed(2)}s / ${duration.toFixed(2)}s`, 4, 16);
    if (p < 1) requestAnimationFrame(frame);
  })(start);
}

// ---- spectrum ------------------------------------------------------------
const sp = $("spectrum");
function drawSpectrum() {
  const ctx = sp.getContext("2d");
  const bins = analyser.frequencyBinCount;
  const data = new Uint8Array(bins);
  const nyquist = audioCtx.sampleRate / 2;
  (function frame() {
    requestAnimationFrame(frame);
    sp.width = sp.clientWidth;
    analyser.getByteFrequencyData(data);
    ctx.clearRect(0, 0, sp.width, sp.height);
    const maxHz = 3000, maxBin = Math.floor((maxHz / nyquist) * bins);
    const bw = sp.width / maxBin;
    ctx.fillStyle = "#00d4ff";
    for (let i = 0; i < maxBin; i++) {
      const h = (data[i] / 255) * sp.height;
      ctx.fillRect(i * bw, sp.height - h, bw + 0.5, h);
    }
    ctx.strokeStyle = "#2a3866";
    ctx.fillStyle = "#8892b0";
    ctx.font = "10px monospace";
    for (let hz = 500; hz <= maxHz; hz += 500) {
      const x = (hz / maxHz) * sp.width;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, sp.height); ctx.stroke();
      ctx.fillText(hz, x + 2, sp.height - 2);
    }
  })();
}

// ---- presets (localStorage primary, server sync when available) ----------

function renderPresets(presets) {
  const ul = $("presets");
  ul.innerHTML = "";
  for (const p of presets) {
    const li = document.createElement("li");
    li.textContent = p.name;
    li.title = p.description || "";
    li.onclick = () => {
      $("digits").value = p.digits || "";
      for (const k of TIMING) if (p.config && p.config[k] != null) $(k).value = p.config[k];
      $("seize_only").checked = !!(p.config && p.config.seize_only);
    };
    ul.appendChild(li);
  }
}

async function loadPresets() {
  let presets = [];
  try {
    const r = await fetch("/api/presets");
    if (r.ok) {
      presets = (await r.json()).presets;
      localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
    }
  } catch {
    const cached = localStorage.getItem(PRESETS_KEY);
    if (cached) presets = JSON.parse(cached);
  }
  renderPresets(presets);
}

async function savePreset() {
  const name = prompt("Preset name:");
  if (!name) return;
  showError("");
  const preset = { name, digits: $("digits").value, config: readConfig(), description: "" };

  // Always save locally first.
  const cached = JSON.parse(localStorage.getItem(PRESETS_KEY) || "[]");
  const idx = cached.findIndex(p => p.name === name);
  if (idx >= 0) cached[idx] = preset; else cached.push(preset);
  localStorage.setItem(PRESETS_KEY, JSON.stringify(cached));

  // Sync to server if available.
  fetch("/api/presets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(preset),
  }).catch(() => {});

  loadPresets();
}

async function loadDevices() {
  try {
    const d = await fetch("/api/devices").then(r => r.json());
    $("devices").textContent =
      `${d.backend}: ` + (d.devices.map(x => x.name).join(", ") || "none");
  } catch {
    $("devices").textContent = "server offline";
  }
}

// ---- service worker registration -----------------------------------------

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// ---- wiring --------------------------------------------------------------

document.querySelector(".keypad").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const inp = $("digits");
  const { act, digit, tone } = b.dataset;

  if (tone === "seize") {
    kickAudio();
    playSeizeTone();
  } else if (digit) {
    inp.value += digit;
    if ($("liveMode").checked) {
      kickAudio();
      playDigitTone(digit);
    }
  } else if (act === "clear") {
    inp.value = "";
  } else if (act === "back") {
    inp.value = inp.value.slice(0, -1);
  }
});

$("play").onclick = playLocal;
$("download").onclick = download;
$("savePreset").onclick = savePreset;
$("serverPlay").onclick = serverPlay;

for (const id of ["digits", ...TIMING, "seize_only"]) {
  const el = $(id);
  if (el) el.addEventListener("change", playLiveDebounced);
  if (el && el.type !== "checkbox") el.addEventListener("input", playLiveDebounced);
}

(async function init() {
  try {
    const h = await fetch("/api/health").then(r => r.json());
    setStatus("connected · " + h.audio, "ok");
  } catch {
    setStatus("offline", "bad");
  }
  loadPresets();
  loadDevices();
})();
