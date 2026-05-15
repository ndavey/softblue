"use strict";

const $ = (id) => document.getElementById(id);
const TIMING = ["seize_duration", "wink_delay", "digit_duration",
                "inter_digit_gap", "amplitude", "sample_rate"];

let livePlaybackTimeout;
const LIVE_DEBOUNCE_MS = 300;

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

function b64ToBuffer(b64) {
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

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

// Must be called synchronously inside a user-gesture handler (before any
// await) so the browser allows resume(). After an await the gesture is gone.
function kickAudio() {
  ensureAudio();
  audioCtx.resume(); // fire-and-forget; already running = no-op
}

// ---- API -----------------------------------------------------------------
async function fetchGenerate(digits, configOverride) {
  const cfg = Object.assign(readConfig(), configOverride || {});
  const res = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digits: digits !== undefined ? digits : $("digits").value, config: cfg }),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(e.detail || "generation failed");
  }
  return res.json();
}

async function decodeAndPlay(data) {
  const audioBuffer = await audioCtx.decodeAudioData(b64ToBuffer(data.audio));
  const src = audioCtx.createBufferSource();
  src.buffer = audioBuffer;
  src.connect(analyser);
  src.start();
  if (data.duration) animateTimeline(data.duration);
}

// ---- play modes ----------------------------------------------------------

// Full sequence from the digits input — called by the Generate & Play button.
async function playLocal() {
  showError("");
  kickAudio(); // MUST be before any await
  try {
    const data = await fetchGenerate();
    await decodeAndPlay(data);
  } catch (e) {
    showError(e.message);
  }
}

// Single-digit live tone — short KP+digit+ST burst, no seize/wink.
async function playDigitTone(digit) {
  try {
    const data = await fetchGenerate(digit, {
      seize_duration: 0, wink_delay: 0,
      kp_duration: 0.05, inter_digit_gap: 0.02,
      digit_duration: 0.1, st_duration: 0.05,
      seize_only: false,
    });
    await decodeAndPlay(data);
  } catch (e) {
    console.error("Tone play error:", e);
  }
}

// Seize-only tone — uses current seize_duration from inputs.
async function playSeizeTone() {
  try {
    const data = await fetchGenerate("", { seize_only: true });
    await decodeAndPlay(data);
  } catch (e) {
    console.error("Seize play error:", e);
  }
}

// Debounced full-sequence replay triggered by input changes.
function playLiveDebounced() {
  if (!$("liveMode").checked) return;
  kickAudio(); // while still inside the change-event user gesture
  clearTimeout(livePlaybackTimeout);
  livePlaybackTimeout = setTimeout(() => playLocal(), LIVE_DEBOUNCE_MS);
}

// ---- download ------------------------------------------------------------
async function download() {
  showError("");
  try {
    const data = await fetchGenerate();
    const blob = new Blob([b64ToBuffer(data.audio)], { type: "audio/wav" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = ($("digits").value || "seize") + ".wav";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { showError(e.message); }
}

// ---- server play ---------------------------------------------------------
async function serverPlay() {
  showError("");
  const res = await fetch("/api/play", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digits: $("digits").value, config: readConfig() }),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: res.statusText }));
    showError(e.detail || "server play failed");
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

// ---- presets & devices ---------------------------------------------------
async function loadPresets() {
  const { presets } = await (await fetch("/api/presets")).json();
  const ul = $("presets");
  ul.innerHTML = "";
  for (const p of presets) {
    const li = document.createElement("li");
    li.textContent = p.name;
    li.title = p.description || "";
    li.onclick = () => {
      $("digits").value = p.digits || "";
      for (const k of TIMING) if (p.config[k] != null) $(k).value = p.config[k];
      $("seize_only").checked = !!p.config.seize_only;
    };
    ul.appendChild(li);
  }
}

async function savePreset() {
  const name = prompt("Preset name:");
  if (!name) return;
  showError("");
  const res = await fetch("/api/presets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, digits: $("digits").value, config: readConfig() }),
  });
  if (res.ok) loadPresets();
  else {
    const e = await res.json().catch(() => ({ detail: "save failed" }));
    showError(e.detail);
  }
}

async function loadDevices() {
  const d = await (await fetch("/api/devices")).json();
  $("devices").textContent =
    `${d.backend}: ` + (d.devices.map((x) => x.name).join(", ") || "none");
}

// ---- wiring --------------------------------------------------------------
document.querySelector(".keypad").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const inp = $("digits");
  const act = b.dataset.act;
  const digit = b.dataset.digit;
  const tone = b.dataset.tone;

  if (tone === "seize") {
    kickAudio(); // synchronous, inside click gesture
    playSeizeTone();
  } else if (digit) {
    inp.value += digit;
    if ($("liveMode").checked) {
      kickAudio(); // synchronous, inside click gesture
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

// Live playback on any input/parameter change.
for (const id of ["digits", ...TIMING, "seize_only"]) {
  const el = $(id);
  if (el) el.addEventListener("change", playLiveDebounced);
  if (el && el.type !== "checkbox") el.addEventListener("input", playLiveDebounced);
}

(async function init() {
  try {
    const h = await (await fetch("/api/health")).json();
    setStatus("connected · " + h.audio, "ok");
  } catch { setStatus("offline", "bad"); }
  loadPresets();
  loadDevices();
})();
