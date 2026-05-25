"use strict";

const $ = (id) => document.getElementById(id);
const TIMING = ["seize_duration", "wink_delay", "digit_duration",
                "inter_digit_gap", "amplitude", "sample_rate"];
const PRESETS_KEY = "softblue-presets";

let currentMode = "mf_r1";
let liveDebounce;
const LIVE_MS = 300;

// ---- mode definitions ----------------------------------------------------
// Each entry drives the keypad layout, the hint line, and validation.
const MODE_DEFS = {
  mf_r1: {
    hint: "MF/R1 (Bell): 0-9 · k=KP · s=ST · z=2600 seize · x=clear · .=idle. " +
          "Plain digits auto-wrap KP…ST.",
    keys: [
      ["1","2","3"],
      ["4","5","6"],
      ["7","8","9"],
      [{act:"clear",label:"C"}, "0", {act:"back",label:"←"}],
      [{ctl:"k",label:"KP"}, {ctl:"s",label:"ST"}, {ctl:".",label:"IDLE"}],
      [{tone:"seize",label:"SEIZE",cls:"seize-btn",span:3}],
    ],
  },
  c5: {
    hint: "CCITT #5: 0-9 · k=KP · s=ST · z=2400 seize · x=clear.",
    keys: [
      ["1","2","3"],
      ["4","5","6"],
      ["7","8","9"],
      [{act:"clear",label:"C"}, "0", {act:"back",label:"←"}],
      [{ctl:"k",label:"KP"}, {ctl:"s",label:"ST"}, {ctl:"x",label:"CLR"}],
      [{tone:"seize",label:"SEIZE 2400",cls:"seize-btn",span:3}],
    ],
  },
  dtmf: {
    hint: "DTMF touch-tone: 0-9 · * · # · A-D.",
    keys: [
      ["1","2","3","A"],
      ["4","5","6","B"],
      ["7","8","9","C"],
      ["*","0","#","D"],
      [{act:"clear",label:"C",span:2}, {act:"back",label:"←",span:2}],
    ],
    grid: 4,
  },
  us_redbox: {
    hint: "US payphone coin tones. 1=nickel · 2=dime · 3=quarter · 4=dollar. " +
          "Scheme switches between real Bell ACTS and PhreakMe-emulated single-tone.",
    keys: [
      [{digit:"1",label:"5¢"}, {digit:"2",label:"10¢"}, {digit:"3",label:"25¢"}, {digit:"4",label:"$1"}],
      [{act:"clear",label:"C",span:2}, {act:"back",label:"←",span:2}],
    ],
    grid: 4,
  },
  uk_redbox: {
    hint: "UK trunk pips. 1 = 10p (200ms) · 2 = 50p (350ms).",
    keys: [
      [{digit:"1",label:"10p"}, {digit:"2",label:"50p"}],
      [{act:"clear",label:"C"}, {act:"back",label:"←"}],
    ],
    grid: 2,
  },
  pulse_2600: {
    hint: "2600 Hz dial pulse / whistle method. Each digit emits N pulses.",
    keys: [
      ["1","2","3"],
      ["4","5","6"],
      ["7","8","9"],
      [{act:"clear",label:"C"}, "0", {act:"back",label:"←"}],
    ],
  },
};

function readConfig() {
  const c = {};
  for (const k of TIMING) c[k] = parseFloat($(k).value);
  c.seize_only = $("seize_only").checked;
  c.mode = currentMode;
  c.coin_scheme = $("coin_scheme").value;
  return c;
}

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = "badge" + (cls ? " " + cls : "");
}

function showError(msg) { $("error").textContent = msg || ""; }

// ---- mode UI -------------------------------------------------------------

function renderKeypad() {
  const def = MODE_DEFS[currentMode];
  const kp = $("keypad");
  kp.innerHTML = "";
  kp.style.gridTemplateColumns = `repeat(${def.grid || 3}, 1fr)`;
  for (const row of def.keys) {
    for (const cell of row) {
      const b = document.createElement("button");
      if (typeof cell === "string") {
        b.textContent = cell;
        b.dataset.digit = cell;
      } else {
        if (cell.digit) { b.dataset.digit = cell.digit; }
        if (cell.act)   { b.dataset.act = cell.act; }
        if (cell.tone)  { b.dataset.tone = cell.tone; }
        if (cell.ctl)   { b.dataset.ctl = cell.ctl; b.classList.add("kp-control"); }
        if (cell.cls)   { b.classList.add(...cell.cls.split(" ")); }
        if (cell.span)  { b.classList.add("span" + cell.span); }
        b.textContent = cell.label || cell.digit || cell.act;
      }
      kp.appendChild(b);
    }
  }
  $("modeHint").textContent = def.hint;
  $("coinSchemeWrap").style.display = currentMode === "us_redbox" ? "" : "none";
  // The "Seize only" checkbox only makes sense in MF/C5.
  const seizeOnlyRow = $("seize_only").parentElement;
  seizeOnlyRow.style.display =
    (currentMode === "mf_r1" || currentMode === "c5") ? "" : "none";
}

function setMode(mode) {
  if (!MODE_DEFS[mode]) return;
  currentMode = mode;
  document.querySelectorAll("#modes button").forEach(b =>
    b.classList.toggle("selected", b.dataset.mode === mode));
  renderKeypad();
  // Sensible default content for unfamiliar modes.
  const defaults = {
    mf_r1: "8675309", c5: "8675309", dtmf: "18005551212",
    us_redbox: "3", uk_redbox: "12", pulse_2600: "0",
  };
  $("digits").value = defaults[mode];
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

function kickAudio() { ensureAudio(); audioCtx.resume(); }

// ---- playback ------------------------------------------------------------

function playLocal() {
  showError("");
  kickAudio();
  try {
    const cfg = readConfig();
    validateDigits($("digits").value, cfg.mode);
    const dur = playSequenceLive(audioCtx, analyser, $("digits").value, cfg);
    animateTimeline(dur);
  } catch (e) {
    showError(e.message);
  }
}

function playSingle(ch) {
  // Quick feedback tone for one keypress. Reuses the per-mode schedule with
  // seize/auto-wrap stripped so it produces just the one event.
  const base = readConfig();
  const cfg = Object.assign({}, base, {
    seize_duration: 0, wink_delay: 0, kp_duration: 0,
    st_duration: 0, seize_only: false,
    inter_digit_gap: 0,
  });
  try {
    playSequenceLive(audioCtx, analyser, ch, cfg);
  } catch (e) { showError(e.message); }
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

// ---- WAV download --------------------------------------------------------

async function download() {
  showError("");
  try {
    const cfg = readConfig();
    validateDigits($("digits").value, cfg.mode);
    const buf = await renderToWav($("digits").value, cfg);
    const blob = new Blob([buf], { type: "audio/wav" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (currentMode + "_" + ($("digits").value || "seize")) + ".wav";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { showError(e.message); }
}

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

// ---- presets -------------------------------------------------------------

function renderPresets(presets) {
  const ul = $("presets");
  ul.innerHTML = "";
  for (const p of presets) {
    const li = document.createElement("li");
    li.textContent = p.name + (p.config && p.config.mode && p.config.mode !== "mf_r1"
                                ? ` [${p.config.mode}]` : "");
    li.title = p.description || "";
    li.onclick = () => {
      if (p.config && p.config.mode) setMode(p.config.mode);
      $("digits").value = p.digits || "";
      for (const k of TIMING) if (p.config && p.config[k] != null) $(k).value = p.config[k];
      $("seize_only").checked = !!(p.config && p.config.seize_only);
      if (p.config && p.config.coin_scheme) $("coin_scheme").value = p.config.coin_scheme;
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
  const cached = JSON.parse(localStorage.getItem(PRESETS_KEY) || "[]");
  const idx = cached.findIndex(p => p.name === name);
  if (idx >= 0) cached[idx] = preset; else cached.push(preset);
  localStorage.setItem(PRESETS_KEY, JSON.stringify(cached));
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

// ---- coin tones (standalone card) ---------------------------------------

let selectedCoin = null;

function playCoin(denomination, slot) {
  kickAudio();
  try {
    const scheme = $("coin_scheme").value;
    const dur = playCoinLive(audioCtx, analyser, denomination, slot,
                             parseFloat($("amplitude").value), scheme);
    animateTimeline(dur);
  } catch (e) {
    showError(e.message);
  }
}

async function downloadCoin() {
  if (!selectedCoin) { showError("Select a coin first."); return; }
  showError("");
  try {
    const buf = await renderCoinToWav(
      selectedCoin.denomination, selectedCoin.slot,
      parseFloat($("amplitude").value),
      parseInt($("sample_rate").value),
      $("coin_scheme").value,
    );
    const blob = new Blob([buf], { type: "audio/wav" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${selectedCoin.slot}_${selectedCoin.denomination}.wav`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { showError(e.message); }
}

document.querySelectorAll(".coin-btns button").forEach(btn => {
  btn.addEventListener("click", () => {
    const { coin, slot } = btn.dataset;
    document.querySelectorAll(".coin-btns button").forEach(b => b.classList.remove("selected"));
    btn.classList.add("selected");
    selectedCoin = { denomination: coin, slot };
    $("coinSelected").textContent = `${slot === "1slot" ? "1-slot" : "3-slot"} · ${coin}`;
    kickAudio();
    playCoin(coin, slot);
  });
});

$("coinDownload").onclick = downloadCoin;

// ---- service worker ------------------------------------------------------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// ---- wiring --------------------------------------------------------------

$("modes").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]");
  if (b) { setMode(b.dataset.mode); playLiveDebounced(); }
});

document.addEventListener("click", (e) => {
  const b = e.target.closest("#keypad button");
  if (!b) return;
  const inp = $("digits");
  const { act, digit, tone, ctl } = b.dataset;

  if (tone === "seize") {
    kickAudio();
    playSeizeTone();
  } else if (ctl) {
    inp.value += ctl;
    if ($("liveMode").checked) { kickAudio(); playSingle(ctl); }
  } else if (digit) {
    inp.value += digit;
    if ($("liveMode").checked) { kickAudio(); playSingle(digit); }
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

for (const id of ["digits", ...TIMING, "seize_only", "coin_scheme"]) {
  const el = $(id);
  if (el) el.addEventListener("change", playLiveDebounced);
  if (el && el.type !== "checkbox" && el.tagName !== "SELECT")
    el.addEventListener("input", playLiveDebounced);
}

renderKeypad();

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
