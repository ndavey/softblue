"use strict";

const $ = (id) => document.getElementById(id);
const TIMING = ["seize_duration", "wink_delay", "digit_duration",
                "kp_duration", "st_duration",
                "inter_digit_gap", "amplitude", "sample_rate"];
// Bell R1 / standard bluebox defaults — used when the matching field is
// missing from the DOM (older cached page) or NaN, so KP/ST always have a
// nonzero duration and actually emit a tone.
const TIMING_DEFAULTS = {
  seize_duration: 2.0, wink_delay: 0.5, digit_duration: 0.06,
  kp_duration: 0.1, st_duration: 0.1, inter_digit_gap: 0.1,
  amplitude: 0.7, sample_rate: 8000,
};
const PRESETS_KEY = "softblue-presets";

let currentMode = "mf_r1";
let liveDebounce;
const LIVE_MS = 300;

// Whether an optional Python backend is reachable. Determined once at startup
// by probeServer(). When false (the normal case for a static PWA install),
// localStorage is the source of truth and all server writes are skipped.
let serverAvailable = false;

// ---- sweep + mic state (isolated from main sequence config) ---------------
let lockedSeizeHz = null;   // null = engine default; only set via sweep lock
let micStream = null;
let micSource = null;
let micNode = null;
let sweepRunning = false;
let sweepCurrentHz = null;

// ---- mode definitions ----------------------------------------------------
// Each entry drives the keypad layout, the hint line, and validation.
const MODE_DEFS = {
  mf_r1: {
    hint: "MF/R1: 0-9 · KP / ST · x=clear · .=idle. Plain digits auto-wrap KP…ST. ▶ SEIZE plays live.",
    keys: [
      ["1","2","3"],
      ["4","5","6"],
      ["7","8","9"],
      [{act:"clear",label:"C"}, "0", {act:"back",label:"←"}],
      [{ctl:"k",label:"KP"}, {ctl:"s",label:"ST"}, {ctl:".",label:"·"}],
      [{ctl:"x",label:"x·CLR",cls:"kp-control"}, {act:"clear",label:"C"}, {act:"back",label:"←"}],
      [{tone:"seize",label:"▶ SEIZE (live 2600)",cls:"seize-btn",span:3}],
    ],
  },
  c5: {
    hint: "CCITT #5: 0-9 · KP / ST · x=clear char · .=idle. ▶ SEIZE plays live.",
    keys: [
      ["1","2","3"],
      ["4","5","6"],
      ["7","8","9"],
      [{act:"clear",label:"C"}, "0", {act:"back",label:"←"}],
      [{ctl:"k",label:"KP"}, {ctl:"s",label:"ST"}, {ctl:".",label:"·"}],
      [{ctl:"x",label:"x·CLR",cls:"kp-control"}, {act:"clear",label:"C"}, {act:"back",label:"←"}],
      [{tone:"seize",label:"▶ SEIZE (live 2400)",cls:"seize-btn",span:3}],
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
  for (const k of TIMING) {
    const el = $(k);
    const v = el ? parseFloat(el.value) : NaN;
    c[k] = (isFinite(v) && v >= 0) ? v : TIMING_DEFAULTS[k];
  }
  c.seize_only = $("seize_only").checked;
  c.mode = currentMode;
  c.coin_scheme = $("coin_scheme").value;
  // seize_freq comes only from an explicit sweep lock — never from timing panel
  c.seize_freq = lockedSeizeHz;
  c.mf_variant = $("mf_variant") ? $("mf_variant").value : "standard";
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
  // MF-only controls
  const isMF = currentMode === "mf_r1" || currentMode === "c5";
  $("mf_variant_wrap").style.display = isMF ? "" : "none";
  $("sweepCard").style.display = isMF ? "" : "none";
  // The "Seize only" checkbox only makes sense in MF/C5.
  const seizeOnlyRow = $("seize_only").parentElement;
  seizeOnlyRow.style.display = isMF ? "" : "none";
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
    captureStepIfRecording();
  } catch (e) {
    showError(e.message);
  }
}

function playSingle(ch) {
  // Quick feedback for one keypress: play *only* the tone for that key,
  // never the seize/KP/ST wrap. `no_wrap` forces literal mode so a plain
  // digit doesn't get KP+ST glued on either side.
  const base = readConfig();
  const fallback = (isFinite(base.digit_duration) && base.digit_duration > 0)
                   ? base.digit_duration : 0.1;
  const cfg = Object.assign({}, base, {
    no_wrap: true,
    seize_duration: fallback,  // for "z" keypress, kept short for live feedback
    wink_delay: 0,
    seize_only: false,
    inter_digit_gap: 0,
    kp_duration: fallback,  // for "k" keypress
    st_duration: fallback,  // for "s" keypress
  });
  try {
    playSequenceLive(audioCtx, analyser, ch, cfg);
  } catch (e) { showError(e.message); }
}

function playSeizeTone() {
  const cfg = Object.assign(readConfig(), { seize_only: true });
  playSequenceLive(audioCtx, analyser, "", cfg);
}

function playLivePreview() {
  // Live preview = the *content* of the digit field, fast. Skip the auto
  // seize/wink/KP/ST wrap (MF/C5) so a single keystroke is heard immediately
  // instead of after a 2-second seize tone.
  showError("");
  kickAudio();
  try {
    const cfg = Object.assign(readConfig(), {
      seize_duration: 0, wink_delay: 0, kp_duration: 0, st_duration: 0,
      seize_only: false,
    });
    validateDigits($("digits").value, cfg.mode);
    const dur = playSequenceLive(audioCtx, analyser, $("digits").value, cfg);
    animateTimeline(dur);
  } catch (e) {
    showError(e.message);
  }
}

function playLiveDebounced() {
  if (!$("liveMode").checked) return;
  kickAudio();
  clearTimeout(liveDebounce);
  liveDebounce = setTimeout(() => playLivePreview(), LIVE_MS);
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
  // localStorage is the source of truth offline; the server (if present) is an
  // optional sync that refreshes the local copy.
  let presets = JSON.parse(localStorage.getItem(PRESETS_KEY) || "[]");
  if (serverAvailable) {
    try {
      const r = await fetch("/api/presets");
      if (r.ok) {
        presets = (await r.json()).presets;
        localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
      }
    } catch { /* keep local copy */ }
  }
  presetsCache = presets;
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
  if (serverAvailable) {
    fetch("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(preset),
    }).catch(() => {});
  }
  loadPresets();
}

async function loadDevices() {
  if (!serverAvailable) return;  // Device card is hidden without a backend.
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

// ---- macros --------------------------------------------------------------

const MACROS_KEY = "softblue-macros";
const MODES_LIST = Object.keys(MODE_DEFS);
let macros = [];
let presetsCache = [];
let recording = false;
let recordBuffer = [];
let editingMacro = null;   // { name, ...} when editing; null when creating

function presetLookupMap() {
  const m = {};
  for (const p of presetsCache) m[p.name] = p;
  return m;
}

function renderMacros() {
  const countEl = $("macroCount");
  if (countEl) countEl.textContent = macros.length ? `(${macros.length})` : "";
  const ul = $("macros");
  ul.innerHTML = "";
  if (!macros.length) {
    const li = document.createElement("li");
    li.style.display = "block";
    li.className = "muted";
    li.textContent = "No macros yet — record one or click + New.";
    ul.appendChild(li);
  }
  for (const m of macros) {
    const li = document.createElement("li");
    li.innerHTML = `
      <button class="pin-toggle ${m.pinned ? "pinned" : ""}" title="Pin to top bar">★</button>
      <button class="play-btn" title="Run">▶</button>
      <span class="name"></span>
      <span class="meta"></span>
      <span class="row-actions">
        <button class="edit-btn" title="Edit">✎</button>
        <button class="del-btn" title="Delete">×</button>
      </span>`;
    li.querySelector(".name").textContent = m.name;
    li.querySelector(".meta").textContent =
      `${(m.steps || []).length} step${(m.steps || []).length === 1 ? "" : "s"}` +
      (m.description ? ` · ${m.description}` : "");
    li.querySelector(".pin-toggle").onclick = () => togglePin(m);
    li.querySelector(".play-btn").onclick = () => runMacro(m);
    li.querySelector(".edit-btn").onclick = () => openMacroEditor(m);
    li.querySelector(".del-btn").onclick = () => deleteMacro(m);
    ul.appendChild(li);
  }
  renderPinnedBar();
}

function renderPinnedBar() {
  const bar = $("pinnedBar");
  bar.innerHTML = "";
  const pinned = macros.filter(m => m.pinned).slice(0, 4);
  if (!pinned.length) { bar.hidden = true; return; }
  bar.hidden = false;
  for (const m of pinned) {
    const b = document.createElement("button");
    b.innerHTML = `<span class="star">★</span>${m.name}`;
    b.onclick = () => runMacro(m);
    bar.appendChild(b);
  }
}

function runMacro(m) {
  showError("");
  kickAudio();
  try {
    const dur = playMacroLive(audioCtx, analyser, m.steps || [], readConfig(),
                              presetLookupMap());
    animateTimeline(dur);
  } catch (e) { showError(`${m.name}: ${e.message}`); }
}

async function togglePin(m) {
  m.pinned = !m.pinned;
  await saveMacro(m, /*silent=*/true);
  renderMacros();
}

async function deleteMacro(m) {
  if (!confirm(`Delete macro "${m.name}"?`)) return;
  macros = macros.filter(x => x.name !== m.name);
  cacheMacrosToLocal();
  renderMacros();
  if (serverAvailable) {
    fetch(`/api/macros/${encodeURIComponent(m.name)}`, { method: "DELETE" })
      .catch(() => {});
  }
}

function cacheMacrosToLocal() {
  localStorage.setItem(MACROS_KEY, JSON.stringify(macros));
}

async function loadMacros() {
  macros = JSON.parse(localStorage.getItem(MACROS_KEY) || "[]");
  if (serverAvailable) {
    try {
      const r = await fetch("/api/macros");
      if (r.ok) {
        macros = (await r.json()).macros;
        cacheMacrosToLocal();
      }
    } catch { /* keep local copy */ }
  }
  renderMacros();
}

async function saveMacro(m, silent) {
  const idx = macros.findIndex(x => x.name === m.name);
  if (idx >= 0) macros[idx] = m; else macros.push(m);
  cacheMacrosToLocal();
  if (serverAvailable) {
    fetch("/api/macros", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(m),
    }).catch(() => {});
  }
  if (!silent) renderMacros();
}

// ---- recording -----------------------------------------------------------

function toggleRecord() {
  if (!recording) {
    recording = true;
    recordBuffer = [];
    $("recordBtn").classList.add("recording");
    $("recordHint").style.display = "";
    // Auto-open the section so the hint is visible.
    $("macrosDetails").open = true;
  } else {
    recording = false;
    $("recordBtn").classList.remove("recording");
    $("recordHint").style.display = "none";
    if (!recordBuffer.length) return;
    openMacroEditor(null, recordBuffer);
    recordBuffer = [];
  }
}

function captureStepIfRecording() {
  if (!recording) return;
  const cfg = readConfig();
  const step = { mode: cfg.mode, digits: $("digits").value };
  if (cfg.mode === "us_redbox") step.config = { coin_scheme: cfg.coin_scheme };
  recordBuffer.push(step);
}

// ---- macro editor modal --------------------------------------------------

const MODAL_DEFAULTS = { mode: "mf_r1", digits: "", delay_after: 0 };

function openMacroEditor(existing, prefilledSteps) {
  editingMacro = existing;  // null for new
  $("macroModalTitle").textContent = existing ? `Edit: ${existing.name}` : "New Macro";
  $("macroName").value = existing?.name || "";
  $("macroName").disabled = !!existing;
  $("macroDesc").value = existing?.description || "";
  $("macroPinned").checked = !!existing?.pinned;
  $("macroDelete").style.display = existing ? "" : "none";
  $("macroJsonErr").textContent = "";
  const steps = existing?.steps || prefilledSteps || [];
  renderStepList(steps);
  $("macroJson").value = JSON.stringify(steps, null, 2);
  switchTab("steps");
  $("macroModal").classList.add("is-open");
}

function closeMacroEditor() {
  $("macroModal").classList.remove("is-open");
  editingMacro = null;
}

function switchTab(name) {
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("selected", b.dataset.tab === name));
  $("tabSteps").hidden = name !== "steps";
  $("tabJson").hidden  = name !== "json";
  // Keep tabs in sync — if leaving steps, push current steps into JSON.
  if (name === "json") $("macroJson").value = JSON.stringify(readSteps(), null, 2);
}

function renderStepList(steps) {
  const ol = $("stepList");
  ol.innerHTML = "";
  steps.forEach((s, i) => ol.appendChild(stepRow(s, i)));
}

function stepRow(step, i) {
  const li = document.createElement("li");
  const mode = step.preset ? "" : (step.mode || MODAL_DEFAULTS.mode);
  li.innerHTML = `
    <span class="step-idx">${i + 1}.</span>
    <select class="step-mode"></select>
    <input class="step-digits" placeholder="${step.preset ? 'preset: ' + step.preset : 'digits'}">
    <input class="step-delay" type="number" step="0.1" min="0" placeholder="delay">
    <button class="del-step" title="Remove step">×</button>`;
  const sel = li.querySelector(".step-mode");
  for (const m of ["__preset__", ...MODES_LIST]) {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m === "__preset__" ? "preset…" : m;
    sel.appendChild(o);
  }
  sel.value = step.preset ? "__preset__" : mode;
  const digits = li.querySelector(".step-digits");
  digits.value = step.preset || step.digits || "";
  li.querySelector(".step-delay").value = step.delay_after || 0;
  li.querySelector(".del-step").onclick = () => {
    li.remove();
    renumberSteps();
  };
  return li;
}

function renumberSteps() {
  document.querySelectorAll("#stepList li .step-idx")
    .forEach((s, i) => s.textContent = `${i + 1}.`);
}

function readSteps() {
  const steps = [];
  for (const li of document.querySelectorAll("#stepList li")) {
    const mode = li.querySelector(".step-mode").value;
    const val  = li.querySelector(".step-digits").value;
    const delay = parseFloat(li.querySelector(".step-delay").value) || 0;
    if (mode === "__preset__") {
      steps.push({ preset: val, delay_after: delay });
    } else {
      steps.push({ mode, digits: val, delay_after: delay });
    }
  }
  return steps;
}

function saveFromEditor() {
  let steps;
  if (!$("tabJson").hidden) {
    try { steps = JSON.parse($("macroJson").value); }
    catch (e) {
      $("macroJsonErr").textContent = "Invalid JSON: " + e.message;
      return;
    }
    if (!Array.isArray(steps)) {
      $("macroJsonErr").textContent = "Expected a JSON array of steps.";
      return;
    }
  } else {
    steps = readSteps();
  }
  const name = $("macroName").value.trim();
  if (!name) { $("macroJsonErr").textContent = "Name is required."; return; }
  const m = {
    name,
    description: $("macroDesc").value,
    pinned: $("macroPinned").checked,
    steps,
  };
  saveMacro(m);
  closeMacroEditor();
}

async function deleteFromEditor() {
  if (!editingMacro) return;
  if (!confirm(`Delete macro "${editingMacro.name}"?`)) return;
  await deleteMacro(editingMacro);
  closeMacroEditor();
}

// ---- macro wiring --------------------------------------------------------

$("recordBtn").onclick = toggleRecord;
$("newMacroBtn").onclick = () => openMacroEditor(null);
$("macroModalClose").onclick = closeMacroEditor;
$("macroCancel").onclick = closeMacroEditor;
$("macroSave").onclick = saveFromEditor;
$("macroDelete").onclick = deleteFromEditor;
$("addStepBtn").onclick = () => {
  $("stepList").appendChild(stepRow(MODAL_DEFAULTS, $("stepList").children.length));
};
document.querySelectorAll(".tabs button").forEach(b =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));
$("macroModal").addEventListener("click", (e) => {
  if (e.target === $("macroModal")) closeMacroEditor();
});

// ---- mic recording -------------------------------------------------------

async function enableMic() {
  try {
    ensureAudio();
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micSource = audioCtx.createMediaStreamSource(micStream);
    micNode = audioCtx.createAnalyser();
    micNode.fftSize = 1024;
    micSource.connect(micNode);
    $("micStatus").textContent = "Mic: ● live";
    $("micStatus").style.color = "var(--success)";
    runMicMeter();
    return true;
  } catch (e) {
    $("micStatus").textContent = "Mic: " + (e.name === "NotAllowedError" ? "permission denied" : e.message);
    $("micStatus").style.color = "var(--danger)";
    return false;
  }
}

function disableMic() {
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (micSource) { micSource.disconnect(); micSource = null; }
  micNode = null;
  $("micStatus").textContent = "Mic: off";
  $("micStatus").style.color = "";
}

function runMicMeter() {
  const canvas = $("micMeter");
  if (!canvas) return;
  const c = canvas.getContext("2d");
  (function frame() {
    if (!micNode) return;
    requestAnimationFrame(frame);
    const td = new Float32Array(micNode.fftSize);
    micNode.getFloatTimeDomainData(td);
    let rms = 0;
    for (const v of td) rms += v * v;
    rms = Math.sqrt(rms / td.length);
    const threshold = parseFloat($("micThreshold").value) || 0.02;
    canvas.width = canvas.clientWidth;
    c.fillStyle = "#1f2a52";
    c.fillRect(0, 0, canvas.width, canvas.height);
    c.fillStyle = rms > threshold ? "var(--success)" : "var(--accent-cyan)";
    c.fillRect(0, 0, Math.min(canvas.width, rms * canvas.width * 20), canvas.height);
  })();
}

// Sample mic RMS in 50ms bins for durationS seconds.
async function recordMicWindow(durationS) {
  if (!micNode) return null;
  const BIN_MS = 50;
  const numBins = Math.max(1, Math.floor(durationS * 1000 / BIN_MS));
  const bins = [];
  for (let i = 0; i < numBins; i++) {
    await new Promise(r => setTimeout(r, BIN_MS));
    if (!micNode) break;
    const td = new Float32Array(micNode.fftSize);
    micNode.getFloatTimeDomainData(td);
    let rms = 0;
    for (const v of td) rms += v * v;
    bins.push(Math.sqrt(rms / td.length));
  }
  return bins;
}

// Find contiguous runs above threshold; return events with timing + duration.
function detectEvents(bins, threshold) {
  const events = [];
  let inEvt = false, start = 0;
  for (let i = 0; i < bins.length; i++) {
    if (bins[i] > threshold && !inEvt) { inEvt = true; start = i; }
    else if (bins[i] <= threshold && inEvt) {
      inEvt = false;
      events.push({ startMs: start * 50, durMs: (i - start) * 50 });
    }
  }
  if (inEvt) events.push({ startMs: start * 50, durMs: (bins.length - start) * 50 });
  return events;
}

// Mini bar using Unicode blocks for the results table energy column.
function energyBar(bins) {
  if (!bins || !bins.length) return "—";
  const peak = Math.max(...bins, 0.001);
  const CHARS = " ▁▂▃▄▅▆▇█";
  return bins.map(v => CHARS[Math.min(8, Math.floor((v / peak) * 8))]).join("");
}

// ---- seize tone sweep ----------------------------------------------------

function updateLockedDisplay() {
  if (lockedSeizeHz !== null) {
    $("sweepLockedLabel").textContent = `Seize Hz locked: ${lockedSeizeHz} Hz (used by Generate & Play)`;
    $("sweepLockedLabel").style.color = "var(--accent-cyan)";
    $("sweepClearLock").style.display = "";
  } else {
    $("sweepLockedLabel").textContent = "Seize Hz: default (2600 / 2400) — no lock";
    $("sweepLockedLabel").style.color = "";
    $("sweepClearLock").style.display = "none";
  }
}

function addSweepRow(hz, bins, events) {
  $("sweepResults").style.display = "";
  const isWink = events.some(e => e.durMs >= 50 && e.durMs <= 600);
  const tr = document.createElement("tr");
  if (isWink) tr.classList.add("sweep-hit");
  const evText = events.length
    ? events.map(e => `t=${(e.startMs / 1000).toFixed(2)}s (${e.durMs}ms)`).join(", ")
    : "—";
  tr.innerHTML =
    `<td>${hz}</td>` +
    `<td class="energy-col">${bins ? energyBar(bins) : "no mic"}</td>` +
    `<td>${evText}</td>` +
    `<td>${isWink ? "✓" : "—"}</td>`;
  $("sweepResultsBody").appendChild(tr);
}

async function startSweep() {
  const start  = parseFloat($("sweep_start").value);
  const end    = parseFloat($("sweep_end").value);
  const step   = parseFloat($("sweep_step").value);
  const pauseS = parseFloat($("sweep_delay").value) || 2.0;

  if (!isFinite(start) || !isFinite(end) || !isFinite(step) || step <= 0) {
    showError("Invalid sweep range — check Start/End/Step values.");
    return;
  }

  // Clear previous results
  $("sweepResultsBody").innerHTML = "";
  $("sweepResults").style.display = "none";
  showError("");
  sweepRunning = true;
  $("sweepBtn").textContent = "⏹ Stop Sweep";

  const freqs = [];
  if (start <= end) {
    for (let f = start; f <= end + 0.001; f += step) freqs.push(Math.round(f));
  } else {
    for (let f = start; f >= end - 0.001; f -= step) freqs.push(Math.round(f));
  }

  const useMic = $("micEnable").checked && micNode != null;
  const threshold = parseFloat($("micThreshold").value) || 0.02;

  for (let i = 0; i < freqs.length; i++) {
    if (!sweepRunning) break;

    const hz = freqs[i];
    sweepCurrentHz = hz;
    $("sweepStatus").textContent = `Testing: ${hz} Hz  (${i + 1} / ${freqs.length})`;

    kickAudio();
    const cfg = Object.assign(readConfig(), { seize_freq: hz });
    let dur = 0;
    try {
      dur = playSequenceLive(audioCtx, analyser, $("digits").value, cfg);
      animateTimeline(dur);
    } catch (e) { showError(e.message); }

    let bins = null;
    if (useMic) {
      // Wait for tone to finish, then record the listen window.
      await new Promise(r => setTimeout(r, Math.max(0, dur * 1000 + 100)));
      if (!sweepRunning) break;
      bins = await recordMicWindow(pauseS);
    } else {
      await new Promise(r => setTimeout(r, Math.max(500, (dur + pauseS) * 1000)));
    }

    if (!sweepRunning) break;
    const events = detectEvents(bins || [], threshold);
    addSweepRow(hz, bins, events);
  }

  stopSweep(sweepRunning);
}

function stopSweep(finished) {
  sweepRunning = false;
  $("sweepBtn").textContent = "▶ Start Sweep";
  if (finished === true) {
    $("sweepStatus").textContent =
      `Done — ${$("sweep_start").value}–${$("sweep_end").value} Hz swept.`;
  }
}

function lockSweepHz() {
  if (sweepCurrentHz == null) { showError("Run a sweep first."); return; }
  lockedSeizeHz = sweepCurrentHz;
  stopSweep(false);
  $("sweepStatus").textContent = `Locked: ${lockedSeizeHz} Hz`;
  updateLockedDisplay();
}

function clearSweepLock() {
  lockedSeizeHz = null;
  updateLockedDisplay();
}

// ---- service worker ------------------------------------------------------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
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
$("sweepBtn").onclick = () => sweepRunning ? stopSweep(false) : startSweep();
$("sweepLock").onclick = lockSweepHz;
$("sweepClearLock").onclick = clearSweepLock;
$("micEnable").addEventListener("change", async () => {
  if ($("micEnable").checked) {
    const ok = await enableMic();
    if (!ok) $("micEnable").checked = false;
  } else {
    disableMic();
  }
});

for (const id of ["digits", ...TIMING, "seize_only", "coin_scheme"]) {
  const el = $(id);
  if (el) el.addEventListener("change", playLiveDebounced);
  if (el && el.type !== "checkbox" && el.tagName !== "SELECT")
    el.addEventListener("input", playLiveDebounced);
}

renderKeypad();

// Probe the optional backend once with a short timeout. Success → server mode
// (shows audio backend, enables the Device card). Failure → standalone PWA
// mode, which is fully functional: every tone is synthesized in-browser.
async function probeServer() {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    const r = await fetch("/api/health", { signal: ctrl.signal });
    clearTimeout(timer);
    if (r.ok) {
      const h = await r.json();
      serverAvailable = true;
      setStatus("connected · " + h.audio, "ok");
      return;
    }
  } catch { /* no backend — that's fine */ }
  serverAvailable = false;
  setStatus("ready · offline", "ok");
  $("deviceCol").style.display = "none";  // server-only features
}

(async function init() {
  await probeServer();
  await loadPresets();
  loadMacros();
  loadDevices();
})();
