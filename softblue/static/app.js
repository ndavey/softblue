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
    hint: "DTMF touch-tone: 0-9 · * · # · A-D (AUTOVON precedence column).",
    keys: [
      ["1","2","3",{digit:"A",label:"A\nFlashOvr",cls:"autovon-key"}],
      ["4","5","6",{digit:"B",label:"B\nFlash",cls:"autovon-key"}],
      ["7","8","9",{digit:"C",label:"C\nImmed",cls:"autovon-key"}],
      ["*","0","#",{digit:"D",label:"D\nPrio",cls:"autovon-key"}],
      [{act:"clear",label:"C",span:2}, {act:"back",label:"←",span:2}],
    ],
    grid: 4,
  },
  autovon: {
    hint: "AUTOVON — military 4×4 keypad. Red column = precedence: " +
          "A=Flash Override (highest, nuclear/presidential) · " +
          "B=Flash · C=Immediate · D=Priority. Routine calls need no precedence tone.",
    keys: [
      ["1","2","3",{digit:"A",label:"A\nFlash Override",cls:"autovon-key autovon-precedence"}],
      ["4","5","6",{digit:"B",label:"B\nFlash",cls:"autovon-key autovon-precedence"}],
      ["7","8","9",{digit:"C",label:"C\nImmediate",cls:"autovon-key autovon-precedence"}],
      ["*","0","#",{digit:"D",label:"D\nPriority",cls:"autovon-key autovon-precedence"}],
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
  phreakme_coin: {
    hint: "PhreakMe's own coin scheme — NOT Bell ACTS. Nickel and dime are the " +
          "same tone separated only by level, and the quarter is an A→B " +
          "sequence. The amplitude control does not apply here — level carries " +
          "meaning. Pick the frequency pair below.",
    keys: [
      [{digit:"n",label:"5¢"}, {digit:"d",label:"10¢"}, {digit:"q",label:"25¢"}, {digit:"$",label:"$1"}],
      [{digit:"c",label:"collect"}, {digit:"r",label:"return"},
       {act:"clear",label:"C"}, {act:"back",label:"←"}],
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
  bell_3slot: {
    hint: "3-slot payphone gong/bell tones (caller's coin deposit). " +
          "1=nickel (1 ding) · 2=dime (2 dings) · 3=quarter (gong).",
    keys: [
      [{digit:"1",label:"5¢ 1 ding"}, {digit:"2",label:"10¢ 2 dings"}, {digit:"3",label:"25¢ gong"}],
      [{act:"clear",label:"C"}, {act:"back",label:"←"}],
    ],
    grid: 3,
  },
  green_box: {
    hint: "Green box: operator coin-control over the voice path. " +
          "c=collect (700+1100) · r=return (1100+1700) · b=ringback (700+1700). " +
          "Each is preceded by the selected operator-release wink.",
    keys: [
      [{digit:"c",label:"COLLECT"}, {digit:"r",label:"RETURN"}, {digit:"b",label:"RINGBACK"}],
      [{act:"clear",label:"C"}, {act:"back",label:"←"}],
    ],
    grid: 3,
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
  c.green_wink = $("green_wink").value;
  // seize_freq comes only from an explicit sweep lock — never from timing panel
  c.seize_freq = lockedSeizeHz;
  c.mf_variant = $("mf_variant") ? $("mf_variant").value : "standard";
  // A chosen coin scheme replaces the built-in 1700/2200 table wholesale, for
  // local Web Audio and for /api/generate alike — otherwise the preview and the
  // WAV would disagree with what the keypad says it is sending.
  if (currentMode === "phreakme_coin") {
    const spec = activeCoinSpec();
    if (spec) c.coin_spec = spec;
  }
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
  $("greenWinkWrap").style.display = currentMode === "green_box" ? "" : "none";
  $("redboxWrap").style.display = currentMode === "phreakme_coin" ? "" : "none";
  if (currentMode === "phreakme_coin") syncRedbox();
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
    us_redbox: "3", phreakme_coin: "q", uk_redbox: "12", pulse_2600: "0",
    bell_3slot: "3", green_box: "r", autovon: "a",
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
      if (p.config && p.config.green_wink) $("green_wink").value = p.config.green_wink;
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
  if (cfg.mode === "green_box") step.config = { green_wink: cfg.green_wink };
  // Without the table, a recorded coin step would replay as last year's 1700/2200
  // even though the macro was captured while a candidate scheme was selected.
  if (cfg.mode === "phreakme_coin" && cfg.coin_spec)
    step.config = { coin_spec: cfg.coin_spec };
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

// ---- theme ---------------------------------------------------------------
const THEME_KEY = "softblue-theme";

// Browser/iOS status-bar tint per theme — keeps the chrome matching the skin.
const THEME_COLORS = { modern: "#00d4ff", bluebox: "#143a6e" };

function applyTheme(t) {
  const root = document.documentElement;
  if (t === "bluebox") root.setAttribute("data-theme", "bluebox");
  else root.removeAttribute("data-theme");
  const btn = $("themeToggle");
  if (btn) btn.textContent = t === "bluebox" ? "▣ 1972" : "▢ Modern";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_COLORS[t] || THEME_COLORS.modern);
  localStorage.setItem(THEME_KEY, t);
}

$("themeToggle").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme") === "bluebox"
    ? "bluebox" : "modern";
  applyTheme(cur === "bluebox" ? "modern" : "bluebox");
};

applyTheme(localStorage.getItem(THEME_KEY) || "modern");

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

const REDBOX_INPUTS = ["rbFreqA", "rbFreqB", "rbDur", "rbGap", "rbNickel", "rbDime"];

for (const id of ["digits", ...TIMING, "seize_only", "coin_scheme", "green_wink",
                  "redboxScheme", ...REDBOX_INPUTS]) {
  const el = $(id);
  if (el) el.addEventListener("change", playLiveDebounced);
  if (el && el.type !== "checkbox" && el.tagName !== "SELECT")
    el.addEventListener("input", playLiveDebounced);
}

// Selecting a candidate re-labels the hint and reveals the custom fields; the
// listener above separately re-plays it when Live is on. Picking one by hand
// also moves the sweep there, so the two never point at different candidates.
$("redboxScheme").addEventListener("change", () => {
  syncRedbox();
  const i = redboxSchemes.findIndex(s => String(s.index) === $("redboxScheme").value);
  if (i >= 0) { rbSweepIdx = i; rbRender(); rbSaveSweep(); }
});

$("rbPlay").onclick = rbPlayQuarter;
$("rbHit").onclick = () => rbMark("hit");
$("rbMiss").onclick = () => rbMark("miss");
$("rbSkip").onclick = () => rbMark("skip");
$("rbReset").onclick = () => {
  if (!confirm("Discard every verdict and start the sweep over?")) return;
  rbResults = {};
  rbGoto(0);
};
$("rbRecord").onclick = () => rbRecord(6);
$("rbAdopt").onclick = rbAdopt;

/* ---- zoom lockout ------------------------------------------------------- */
// CSS touch-action kills double-tap zoom, and the viewport meta covers Android,
// but iOS Safari ignores user-scalable=no — pinch has to be cancelled here.
// These are the WebKit-only gesture events; preventDefault needs a non-passive
// listener. Nothing in the UI uses multi-touch, so nothing else is affected.
for (const ev of ["gesturestart", "gesturechange", "gestureend"])
  document.addEventListener(ev, (e) => e.preventDefault(), { passive: false });

// Pinch on browsers without gesture events. Deliberately touchmove and not
// touchend: cancelling touchend would swallow the click and break fast dialing.
document.addEventListener("touchmove", (e) => {
  if (e.touches.length > 1) e.preventDefault();
}, { passive: false });

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


// ---- SIP ------------------------------------------------------------------
// The browser has no UDP; /api/sip/call runs the call server-side. Credentials
// never reach this page — we only ever learn whether they are configured.

let sipWavB64 = null;

function sipErr(msg) { $("sipError").textContent = msg || ""; }

// PhreakMe's coin table is generated from one frequency pair, so a change of
// frequencies moves only that pair. One ordered candidate list drives both the
// keypad (local Web Audio, held against the handset) and the SIP card
// (server-side dial) so the two can never disagree about what scheme N is.
//
// The server's list is the ranked analysis and is preferred; the built-in
// enumeration is the offline fallback, because this page has to work on a phone
// with no backend reachable.
let redboxSchemes = [];

async function loadRedboxSchemes() {
  redboxSchemes = redboxCandidates();
  if (serverAvailable) {
    try {
      const d = await (await fetch("/api/redbox/schemes")).json();
      if (d.schemes && d.schemes.length) redboxSchemes = d.schemes;
    } catch { /* keep the offline enumeration */ }
  }

  const sip = $("sipRedboxScheme");
  const keypad = $("redboxScheme");
  for (const s of redboxSchemes) {
    if (sip) {
      const o = document.createElement("option");
      o.value = String(s.index);
      o.textContent = `${s.index}. ${s.describe}`;
      sip.appendChild(o);
    }
    const o2 = document.createElement("option");
    o2.value = String(s.index);
    o2.textContent = `${s.index}. ${s.freq_a}→${s.freq_b} Hz`
      + `, ${Math.round((s.duration ?? 0.06) * 1000)}ms`
      + (s.is_control ? "  — last year's table" : "")
      + (s.confidence ? `  (${s.confidence})` : "");
    keypad.appendChild(o2);
  }
  const custom = document.createElement("option");
  custom.value = "custom";
  custom.textContent = "Custom… (enter the pair yourself)";
  keypad.appendChild(custom);

  // Resume where the sweep left off — a con floor is not a place to remember
  // which of 45 candidates you had already ruled out.
  rbLoadSweep();
  rbGoto(rbSweepIdx);
}

/** The scheme the keypad is currently set to, as a coin_spec table. */
function activeCoinSpec() {
  const sel = $("redboxScheme");
  if (!sel || !sel.value) return null;
  if (sel.value === "custom") {
    const num = (id, fallback) => {
      const v = parseFloat($(id).value);
      return isFinite(v) ? v : fallback;
    };
    return redboxCoinSpec({
      freq_a: num("rbFreqA", 1700), freq_b: num("rbFreqB", 2200),
      duration: num("rbDur", 60) / 1000, gap: num("rbGap", 60) / 1000,
      nickel_dbfs: num("rbNickel", -6), dime_dbfs: num("rbDime", -3),
    });
  }
  const s = redboxSchemes.find(x => String(x.index) === sel.value);
  if (!s) return null;
  // The server ships the rendered table; offline we build it from the pair.
  return s.coin_spec || redboxCoinSpec(s);
}

// ---- scheme sweep ---------------------------------------------------------
//
// The sweep does not hold its own copy of the candidate — it drives the
// selector. So whatever the sweep is pointing at is what Play, Download WAV,
// the SIP card and a recorded macro all use, and there is no way for the thing
// you are auditioning to differ from the thing you send.

const RB_SWEEP_KEY = "softblue-redbox-sweep";
let rbSweepIdx = 0;
let rbResults = {};

function rbLoadSweep() {
  try {
    const s = JSON.parse(localStorage.getItem(RB_SWEEP_KEY) || "{}");
    rbSweepIdx = Number.isInteger(s.idx) ? s.idx : 0;
    rbResults = s.results && typeof s.results === "object" ? s.results : {};
  } catch { rbSweepIdx = 0; rbResults = {}; }
}

function rbSaveSweep() {
  try {
    localStorage.setItem(RB_SWEEP_KEY,
      JSON.stringify({ idx: rbSweepIdx, results: rbResults }));
  } catch { /* private mode — the sweep still works, it just won't resume */ }
}

/** Point the sweep (and therefore the whole page) at candidate `i`. */
function rbGoto(i) {
  if (!redboxSchemes.length) return;
  rbSweepIdx = Math.max(0, Math.min(i, redboxSchemes.length - 1));
  $("redboxScheme").value = String(redboxSchemes[rbSweepIdx].index);
  syncRedbox();
  rbRender();
  rbSaveSweep();
}

function rbRender() {
  const s = redboxSchemes[rbSweepIdx];
  if (!s) return;
  const tried = Object.keys(rbResults).length;
  const hits = Object.values(rbResults).filter(v => v === "hit").length;
  $("rbSweepProgress").textContent =
    `${rbSweepIdx + 1} of ${redboxSchemes.length} · ${tried} tried · ${hits} hit`;

  const mark = rbResults[s.label];
  const badge = mark === "hit" ? "  ✓ HIT" : mark === "miss" ? "  ✗ miss"
              : mark === "skip" ? "  ⏭ skipped" : "";
  $("rbSweepCurrent").textContent = `${s.index}. ${s.freq_a}→${s.freq_b} Hz`
    + `, ${Math.round((s.duration ?? 0.06) * 1000)}ms${badge}`;
  $("rbSweepCurrent").classList.toggle("rb-done", mark === "hit");

  const hitList = Object.entries(rbResults).filter(([, v]) => v === "hit")
    .map(([k]) => k);
  $("rbSweepLog").textContent = hitList.length
    ? `Hits: ${hitList.join(", ")} — select one above and use Download WAV, or `
      + `copy it into the CLI with: softblue redbox spec -f `
      + `${redboxSchemes.find(x => x.label === hitList[0])?.freq_a},`
      + `${redboxSchemes.find(x => x.label === hitList[0])?.freq_b} -o hit.json`
    : tried ? `${tried} tried, none accepted yet.` : "";
}

/** Record a verdict and move to the next candidate that has no verdict yet. */
function rbMark(result) {
  const s = redboxSchemes[rbSweepIdx];
  if (!s) return;
  rbResults[s.label] = result;
  let next = rbSweepIdx + 1;
  while (next < redboxSchemes.length && rbResults[redboxSchemes[next].label]) next++;
  rbGoto(next < redboxSchemes.length ? next : rbSweepIdx);
}

function rbPlayQuarter() {
  showError("");
  kickAudio();
  try {
    // Always the quarter — see the panel copy. Deliberately not $("digits"),
    // which the operator may have left on a nickel from earlier poking.
    const dur = playSequenceLive(audioCtx, analyser, "q", readConfig());
    animateTimeline(dur);
  } catch (e) {
    showError(e.message);
  }
}

// ---- blind capture --------------------------------------------------------
//
// Records raw PCM off the mic and reads a coin scheme back out of it. Uses a
// ScriptProcessorNode: deprecated, but it is the one raw-PCM tap that works on
// every browser this PWA gets installed on, iOS Safari included. An
// AudioWorklet would need a separate module file, which is one more thing to
// cache correctly and one more thing to fail at the con.

let rbCaptured = null;

async function rbRecord(seconds = 6) {
  if (!micNode || !micSource) {
    $("rbCaptureStatus").textContent = "enable the mic first (Wink Detection)";
    return;
  }
  const btn = $("rbRecord");
  btn.disabled = true;
  $("rbAdopt").disabled = true;
  const sr = audioCtx.sampleRate;
  const chunks = [];
  const tap = audioCtx.createScriptProcessor(4096, 1, 1);
  // A ScriptProcessor only runs while connected to the graph. Route it to a
  // muted gain rather than the destination, or the mic feeds back through the
  // speaker — which at a payphone is a howl into the mouthpiece.
  const sink = audioCtx.createGain();
  sink.gain.value = 0;
  tap.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  micSource.connect(tap);
  tap.connect(sink);
  sink.connect(audioCtx.destination);

  try {
    for (let left = seconds; left > 0; left--) {
      $("rbCaptureStatus").textContent = `● recording — ${left}s`;
      await new Promise(r => setTimeout(r, 1000));
    }
  } finally {
    tap.onaudioprocess = null;
    try { micSource.disconnect(tap); } catch {}
    try { tap.disconnect(); sink.disconnect(); } catch {}
    btn.disabled = false;
  }

  const total = chunks.reduce((a, c) => a + c.length, 0);
  const pcm = new Float32Array(total);
  let off = 0;
  for (const c of chunks) { pcm.set(c, off); off += c.length; }
  $("rbCaptureStatus").textContent = `${(total / sr).toFixed(1)}s captured`;
  rbAnalyseCapture(pcm, sr);
}

function rbAnalyseCapture(pcm, sr) {
  const segs = scanCoinSegments(pcm, sr);
  const body = $("rbCaptureBody");
  body.innerHTML = "";
  for (const s of segs) {
    const tr = document.createElement("tr");
    // "noise" and "silence" are different findings: a loud run with no tonal
    // peak means something was there and it wasn't one of the seven.
    const cells = s.silent
      ? [`${s.startS.toFixed(3)}s`, s.noise ? "noise (no tonal peak)" : "silence",
         `${s.durMs.toFixed(0)} ms`, "—"]
      : [`${s.startS.toFixed(3)}s`, s.freqs.join("+") + " Hz",
         `${s.durMs.toFixed(0)} ms`, `${s.relDb.toFixed(1)} dB`];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  $("rbCaptureTable").style.display = segs.length ? "" : "none";

  rbCaptured = inferCoinScheme(segs);
  const v = $("rbCaptureVerdict");
  if (!rbCaptured) {
    v.textContent = segs.length
      ? "No tone at any of the seven detectable frequencies. If the real scheme "
      + "is outside that set, no sweep here can reach it either."
      : "Nothing above the noise floor — move closer or raise the source level.";
    $("rbAdopt").disabled = true;
    return;
  }
  const { freq_a, freq_b, duration, pattern, confident } = rbCaptured;
  // Levels are relative: an acoustic path has arbitrary gain, so the capture
  // can pin frequency, order and duration but never absolute dBFS.
  v.textContent = `Looks like ${pattern}: A=${freq_a} Hz`
    + (freq_b ? `, B=${freq_b} Hz` : ", B unknown from this pattern")
    + `, ${Math.round(duration * 1000)} ms segments.`
    + (confident ? "" : "  Low confidence — capture a quarter or a collect/return "
                      + "if you can; those pin the scheme unambiguously.")
    + "  Levels are relative to the loudest burst, so nickel-vs-dime cannot be "
    + "read off an acoustic capture.";
  $("rbAdopt").disabled = false;
}

/** Push a captured scheme into the Custom fields and select it. */
function rbAdopt() {
  if (!rbCaptured) return;
  $("rbFreqA").value = String(rbCaptured.freq_a);
  if (rbCaptured.freq_b) $("rbFreqB").value = String(rbCaptured.freq_b);
  $("rbDur").value = String(Math.round(rbCaptured.duration * 1000));
  $("redboxScheme").value = "custom";
  syncRedbox();
  $("rbCaptureVerdict").textContent =
    "Adopted as the custom scheme — Play, Download WAV and the SIP card now all "
    + "use it." + (rbCaptured.freq_b ? "" : "  Set Tone B by hand; this pattern "
                                          + "did not reveal it.");
}

/** Reflect the selection in the hint line and reveal the custom fields. */
function syncRedbox() {
  const sel = $("redboxScheme");
  if (!sel) return;
  const isCustom = sel.value === "custom";
  $("redboxCustom").style.display = isCustom ? "" : "none";
  if (isCustom) {
    $("redboxWhy").textContent =
      "Custom pair — nickel and dime ride tone A at the two levels, the quarter "
      + "is A then B, the dollar is A and B together.";
    return;
  }
  const s = redboxSchemes.find(x => String(x.index) === sel.value);
  if (!s) { $("redboxWhy").textContent = ""; return; }
  $("redboxWhy").textContent = s.rationale
    ? s.rationale.slice(0, 240) + (s.rationale.length > 240 ? "…" : "")
    : s.describe || "";
}

async function probeSip() {
  if (!serverAvailable) {
    $("sipStatus").textContent =
      "Needs the Python backend — SIP cannot run from a static page.";
    $("sipDial").disabled = true;
    return;
  }
  try {
    const r = await fetch("/api/sip/status");
    const d = await r.json();
    if (d.configured) {
      const a = d.account;
      $("sipStatus").textContent =
        `${a.user}@${a.host}:${a.port}` +
        (a.register ? " · registers" : " · no REGISTER") +
        (a.has_password ? "" : " · no password configured");
      $("sipDial").disabled = false;
    } else {
      $("sipStatus").textContent = d.detail || "Not configured.";
      $("sipDial").disabled = true;
    }
  } catch {
    $("sipStatus").textContent = "Could not reach the server.";
    $("sipDial").disabled = true;
  }
}

function renderSipSegments(segs) {
  const body = $("sipResultBody");
  body.innerHTML = "";
  for (const s of segs) {
    const tr = document.createElement("tr");
    const cells = s.silent
      ? [`${s.start.toFixed(3)}s`, "—", `${s.dur_ms.toFixed(1)} ms`, "silence"]
      : [`${s.start.toFixed(3)}s`, s.freqs.join("+") + " Hz",
         `${s.dur_ms.toFixed(1)} ms`, `${s.level_dbfs.toFixed(1)} dBFS`];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  $("sipResultTable").style.display = segs.length ? "" : "none";
}

$("sipDial").addEventListener("click", async () => {
  sipErr("");
  const ext = $("sipExtension").value.trim();
  if (!ext) { sipErr("Enter an extension to dial."); return; }

  const body = {
    extension: ext,
    listen: parseFloat($("sipListen").value) || 0,
    wait_before: parseFloat($("sipWaitBefore").value) || 0,
    no_register: $("sipNoRegister").checked,
  };
  const dial = $("sipDialString").value.trim();
  if (dial) body.dial = dial;
  const scheme = $("sipRedboxScheme") ? $("sipRedboxScheme").value : "";
  if (scheme) body.redbox_scheme = parseInt(scheme, 10);
  if ($("sipSendTones").checked) {
    body.digits = $("digits").value;
    body.config = readConfig();
  }

  const btn = $("sipDial");
  btn.disabled = true;
  $("sipBusy").textContent = "dialing…";
  $("sipResultTable").style.display = "none";
  $("sipAudio").style.display = "none";
  $("sipSaveWav").style.display = "none";
  sipWavB64 = null;
  try {
    const r = await fetch("/api/sip/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    $("sipResultMeta").textContent =
      `${d.codec} · ${d.remote} · ${d.duration.toFixed(2)}s received`
      + (d.scheme ? `  ·  scheme ${d.scheme}` : "");
    $("sipTimeline").textContent = (d.timeline || [])
      .map(t => `${t.at.toFixed(2)}s ${t.detail}`).join("  ·  ");
    renderSipSegments(d.segments || []);
    if (d.audio) {
      sipWavB64 = d.audio;
      $("sipAudio").src = "data:audio/wav;base64," + d.audio;
      $("sipAudio").style.display = "";
      $("sipSaveWav").style.display = "";
    } else {
      $("sipResultMeta").textContent += " — no far-end audio (check NAT/comedia)";
    }
  } catch (e) {
    sipErr(e.message);
  } finally {
    btn.disabled = false;
    $("sipBusy").textContent = "—";
  }
});

$("sipSaveWav").addEventListener("click", () => {
  if (!sipWavB64) return;
  const a = document.createElement("a");
  a.href = "data:audio/wav;base64," + sipWavB64;
  a.download = `farend-${$("sipExtension").value.trim() || "call"}.wav`;
  a.click();
});

(async function init() {
  await probeServer();
  await probeSip();
  await loadRedboxSchemes();
  await loadPresets();
  loadMacros();
  loadDevices();
})();
