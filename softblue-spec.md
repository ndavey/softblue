# SoftBlue — Specification Document

> A multi-modal MF (Multi-Frequency) bluebox tone generator for PhreakMe CTF preparation. Features CLI, TUI, and self-hosted web interface.

**Name:** SoftBlue  
**Status:** Specification ready for implementation  
**Target:** Python 3.10+, Linux/macOS/Windows  
**Output:** WAV files, live audio, web-streamed audio  
**Author:** shelldon  
**Date:** 2026-05-15

---

## 1. Overview

SoftBlue generates complete MF signaling sequences for seizing phone trunks and dialing digits. It supports three interface modes:

1. **CLI** — Command-line for scripts and automation
2. **TUI** — Terminal User Interface for interactive use
3. **Web** — Self-hosted web interface for browser-based control

All modes share the same core tone generation engine.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        softblue                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   CLI Mode  │  │   TUI Mode  │  │     Web Mode        │  │
│  │  (argparse) │  │  (textual)  │  │  (fastapi + websockets)│ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                    │              │
│  ┌──────┴────────────────┴────────────────────┴──────────┐  │
│  │                 Core Engine                            │  │
│  │  • Tone Generator (numpy sine synthesis)             │  │
│  │  • Sequence Builder (seize→wink→KP→digits→ST)      │  │
│  │  • Audio Output (WAV / live / websocket stream)     │  │
│  │  • Device Manager (ALSA / PulseAudio / CoreAudio)   │  │
│  │  • Preset Library (JSON storage)                     │  │
│  └────────────────────────┬──────────────────────────────┘  │
│                           │                                  │
│  ┌────────────────────────┴──────────────────────────────┐  │
│  │              Audio Backend                            │  │
│  │  • sounddevice (primary)                              │  │
│  │  • aplay / paplay (fallback)                          │  │
│  │  • Web Audio API (browser streaming)                   │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Installation & Setup

```bash
# Install from PyPI (when published)
pip install softblue

# Or install from source
git clone https://github.com/ndavey/dc34-phreakme
cd labs/01-trunk-seizure/scripts/softblue
pip install -e ".[all]"

# Minimal install (CLI only)
pip install -e ".[cli]"

# TUI install
pip install -e ".[tui]"

# Web install
pip install -e ".[web]"
```

### Dependencies by Mode

| Mode | Required | Optional |
|------|----------|----------|
| Core | `numpy`, `click` | `scipy` (FFT analysis) |
| CLI | `click` (arg parsing) | `rich` (colored output) |
| TUI | `textual` | `textual-dev` (dev tools) |
| Web | `fastapi`, `uvicorn`, `websockets` | `jinja2` (templates) |
| Audio | `sounddevice` | `pyaudio` |

---

## 4. CLI Mode

### 4.1 Commands

```bash
# Generate WAV file
softblue generate 1234 --output call.wav

# Live playback
softblue play 8675309

# List audio devices
softblue devices

# Interactive TUI
softblue tui

# Start web server
softblue web --port 8080

# Save preset
softblue preset save my-preset --digits "1234" --seize 3.0

# Load and play preset
softblue preset load my-preset --play

# Show preset library
softblue preset list

# Verify generated audio (FFT analysis)
softblue verify call.wav
```

### 4.2 Global Options

```bash
softblue [command] [options]

Options:
  --device, -d          Audio device index or name
  --sample-rate, -r     Sample rate (default: 8000)
  --amplitude, -a       Amplitude 0.0-1.0 (default: 0.7)
  --config, -c          Config file path
  --verbose, -v         Verbose output
  --help, -h            Show help
  --version             Show version
```

### 4.3 Generate Command

```bash
softblue generate <digits> [options]

Options:
  --output, -o          Output WAV file
  --seize-duration      Seizure tone duration (default: 2.0)
  --wink-delay          Delay after seizure (default: 0.5)
  --digit-duration      MF digit duration (default: 0.06)
  --inter-digit-gap     Gap between digits (default: 0.1)
  --kp-duration         KP tone duration (default: 0.1)
  --st-duration         ST tone duration (default: 0.1)
```

### 4.4 Play Command

```bash
softblue play <digits> [options]

Options:
  --device              Audio device (default: system default)
  --loop, -l            Loop playback N times
  --countdown, -n       Countdown seconds before playing
```

### 4.5 Examples

```bash
# Basic generation
softblue generate 1234 -o call.wav

# Direct line injection via USB sound card
softblue play 0 --device "plughw:1,0"

# Custom timing for fussy switch
softblue play 8675309 --seize-duration 3 --wink-delay 1.5 --digit-duration 0.08

# Batch generate all digits
for d in {0..9}; do softblue generate $d -o digit_${d}.wav; done

# Save and reuse preset
softblue preset save projectmf-default --digits "1234" --seize 2.5 --wink 0.8
softblue preset load projectmf-default --play
```

---

## 5. TUI Mode (Terminal User Interface)

### 5.1 Launch

```bash
softblue tui
# or
softblue tui --device "plughw:1,0"
```

### 5.2 Layout

```
┌────────────────────────────────────────────────────────────┐
│  SoftBlue v1.0                    Device: Default        │
├──────────────────────────┬─────────────────────────────────┤
│  SEQUENCE BUILDER         │  TIMELINE VISUALIZATION        │
│                           │                                 │
│  ┌─ Seizure ─────────┐   │  [2600Hz]███░░░[KP]░[1]░[2]... │
│  │ Duration: 2.0s    │   │       2.0s   0.5s  0.1s  0.06s  │
│  └───────────────────┘   │                                 │
│                           │  ┌─ Preview ─────────────────┐  │
│  ┌─ Digits ──────────┐   │  │ ▶ Play  ⏹ Stop  🔄 Loop │  │
│  │ Enter: 8675309    │   │  │ Vol: [████████░░] 80%     │  │
│  └───────────────────┘   │  └───────────────────────────┘  │
│                           │                                 │
│  ┌─ Timing ───────────┐  │  ┌─ Device ──────────────────┐  │
│  │ Wink: 0.5s         │  │  │ > Default (Built-in)      │  │
│  │ Digit: 0.06s        │  │  │   USB Audio Device (1)    │  │
│  │ Gap: 0.1s           │  │  │   Null Output             │  │
│  │ KP: 0.1s            │  │  └──────────────────────────┘  │
│  │ ST: 0.1s            │  │                                 │
│  └───────────────────┘   │                                 │
│                           │                                 │
│  [G]enerate  [P]lay      │  [W]eb Start  [H]elp  [Q]uit   │
│  [S]ave Preset [L]oad    │                                 │
├──────────────────────────┴─────────────────────────────────┤
│  Status: Ready  │  Sample Rate: 8000  │  Format: Int16      │
└────────────────────────────────────────────────────────────┘
```

### 5.3 Key Bindings

| Key | Action |
|-----|--------|
| `Tab` | Switch between panels |
| `↑/↓` | Navigate within panel |
| `Enter` | Activate selected item |
| `Space` | Toggle/check |
| `G` | Generate sequence |
| `P` | Play sequence |
| `S` | Save as preset |
| `L` | Load preset |
| `D` | Device selection dialog |
| `E` | Export to WAV |
| `V` | Verify last sequence (FFT) |
| `R` | Record from microphone |
| `W` | Start web server |
| `H` | Help dialog |
| `Q` / `Ctrl+C` | Quit |

### 5.4 Interactive Digit Entry

```
┌─ Enter Digits ───────────────┐
│                                │
│  Digits: 8675309_             │
│                                │
│  [1] [2] [3]                   │
│  [4] [5] [6]                   │
│  [7] [8] [9]                   │
│  [KP] [0] [ST]                 │
│                                │
│  Preview: KP→8→6→7→5→3→0→9→ST │
│                                │
│     [OK]    [Cancel]           │
└────────────────────────────────┘
```

### 5.5 Preset Manager

```
┌─ Presets ──────────────────────┐
│  > projectmf-default             │
│    contest-day                   │
│    slow-switch                   │
│    emergency-test                │
│                                  │
│  [N]ew  [D]elete  [E]dit  [L]oad│
└──────────────────────────────────┘
```

### 5.6 Real-Time Visualization

During playback, show an ASCII oscilloscope:

```
┌─ Playing: 8675309 ─────────────┐
│  │╲    │╱╲    │╱    │╲    │╱   │
│  │ ╲   │  ╲   │╱╲   │ ╲   │    │
│──┼──╲──┼────╲─┼──╲──┼──╲──┼────│
│  │    ╲│      ╲│    ╲│    ╲│    │
│  │     │       │     │     │    │
│  2600Hz  wink   KP    8     6   │
│       2.0s   0.5s  0.1s 0.06s   │
└──────────────────────────────────┘
```

---

## 6. Web Mode (Self-Hosted Interface)

### 6.1 Launch

```bash
# Start web server
softblue web

# Custom port and host
softblue web --port 8080 --host 0.0.0.0

# With auto-open browser
softblue web --open
```

### 6.2 Web Interface Design

**Modern, dark-themed interface inspired by PhreakMe's aesthetic.**

#### Main Page Layout

```
┌─────────────────────────────────────────────────────────────┐
│  🔷 SoftBlue Web                  [Connected]  [Settings]   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─ Sequence Builder ──────────────────────────────────┐   │
│  │                                                      │   │
│  │   Digits to Dial: [ 8675309    ]  [📋 Paste]        │   │
│  │                                                      │   │
│  │   ┌─ Timing ─┐  ┌─ Tone ─────┐  ┌─ Output ─────┐   │   │
│  │   │ Seize:   │  │ Amplitude: │  │ [⚪ WAV  ]   │   │   │
│  │   │ [2.0  s] │  │ [0.7    ]  │  │ [🔵 Live ]   │   │   │
│  │   │ Wink:    │  │ Sample Rate│  │ [⚪ Web  ]   │   │   │
│  │   │ [0.5  s] │  │ [8000   ]  │  └──────────────┘   │   │
│  │   │ Digit:   │  └────────────┘                       │   │
│  │   │ [0.06 s] │                                      │   │
│  │   │ Gap:     │                                      │   │
│  │   │ [0.1  s] │                                      │   │
│  │   └──────────┘                                      │   │
│  │                                                      │   │
│  │   [🔴 Generate & Play]  [💾 Save Preset]           │   │
│  │                                                      │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌─ Timeline ─────────────────────────────────────────┐   │
│  │                                                       │   │
│  │  2600Hz ════════════░░░░ KP ░ 8 ░ 6 ░ 7 ░ 5 ░ 3 ░ 0 ░ 9 ░ ST  │
│  │  0s     1s          2s     2.5 2.6 2.7 2.8 2.9 3.0 3.1 3.2 3.3 │
│  │                                                       │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌─ Presets ──────────────┐  ┌─ Device ─────────────────┐   │
│  │ • projectmf-default    │  │                          │   │
│  │ • contest-day          │  │  [🔊 Default Audio    ▼] │   │
│  │ • slow-switch          │  │                          │   │
│  │ • emergency-test       │  │  [Test Audio] [Refresh]  │   │
│  └────────────────────────┘  └──────────────────────────┘   │
│                                                             │
│  ┌─ Spectrum Analyzer ───────────────────────────────────┐   │
│  │                                                       │   │
│  │   ▲                                                   │   │
│  │   │    ╱╲              ╱╲    ╱╲                      │   │
│  │   │   ╱  ╲            ╱  ╲  ╱  ╲                     │   │
│  │   │──╱────╲──────────╱────╲╱────╲────────────────    │   │
│  │   0   500   1000   1500   2000   2500   3000  Hz     │   │
│  │                                                       │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

#### Color Scheme (PhreakMe-Inspired)

```css
:root {
  --bg-primary: #0a0e27;      /* Deep navy */
  --bg-secondary: #141b3d;     /* Slightly lighter */
  --accent-cyan: #00d4ff;      /* Primary accent */
  --accent-magenta: #ff00a0;   /* Secondary accent */
  --text-primary: #e0e0e0;     /* Main text */
  --text-secondary: #8892b0;  /* Muted text */
  --success: #00ff88;          /* Success states */
  --warning: #ffaa00;          /* Warnings */
  --danger: #ff0044;           /* Errors */
  --tone-2600: #ff0044;        /* Seizure tone color */
  --tone-kp: #ffaa00;          /* KP tone color */
  --tone-digit: #00d4ff;       /* Digit tone color */
  --tone-st: #00ff88;          /* ST tone color */
}
```

### 6.3 Web API Endpoints

```python
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="SoftBlue Web")

# Health check
@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "1.0.0"}

# Generate sequence (returns WAV bytes)
@app.post("/api/generate")
def generate_sequence(request: GenerateRequest):
    """Generate MF sequence and return as WAV bytes."""
    samples = engine.build_sequence(request.digits, request.config)
    wav_bytes = engine.to_wav_bytes(samples)
    return {"audio": base64.b64encode(wav_bytes), "duration": len(samples)/8000}

# Play sequence on server audio device
@app.post("/api/play")
def play_sequence(request: PlayRequest):
    """Play sequence through server's audio device."""
    engine.play(request.digits, request.config, device=request.device)
    return {"status": "playing", "duration": request.duration}

# List audio devices
@app.get("/api/devices")
def list_devices():
    return {"devices": engine.get_devices()}

# Preset management
@app.get("/api/presets")
def list_presets():
    return {"presets": preset_manager.list_all()}

@app.post("/api/presets")
def save_preset(preset: Preset):
    preset_manager.save(preset)
    return {"status": "saved"}

@app.delete("/api/presets/{name}")
def delete_preset(name: str):
    preset_manager.delete(name)
    return {"status": "deleted"}

# Real-time streaming via WebSocket
@app.websocket("/ws/audio")
async def audio_stream(websocket: WebSocket):
    """Stream generated audio in real-time to browser."""
    await websocket.accept()
    while True:
        message = await websocket.receive_json()
        digits = message["digits"]
        config = message.get("config", {})
        
        # Generate and stream chunk by chunk
        for chunk in engine.generate_chunks(digits, config):
            await websocket.send_bytes(chunk)

# Serve static frontend files
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### 6.4 WebSocket Audio Streaming

The web interface streams generated audio in real-time to the browser using WebSocket:

```javascript
// Browser-side WebSocket audio playback
const ws = new WebSocket('ws://localhost:8080/ws/audio');
const audioContext = new AudioContext({sampleRate: 8000});

ws.onmessage = async (event) => {
    const arrayBuffer = await event.data.arrayBuffer();
    const audioBuffer = audioContext.createBuffer(1, arrayBuffer.byteLength / 2, 8000);
    const channelData = audioBuffer.getChannelData(0);
    
    // Convert Int16 to Float32
    const int16Array = new Int16Array(arrayBuffer);
    for (let i = 0; i < int16Array.length; i++) {
        channelData[i] = int16Array[i] / 32768;
    }
    
    const source = audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioContext.destination);
    source.start();
};

// Send generation request
ws.send(JSON.stringify({
    digits: "8675309",
    config: {seize_duration: 2.0, wink_delay: 0.5}
}));
```

### 6.5 Browser Features

- **Tone Pad** — Clickable digit buttons (like a phone keypad)
- **Client-side synthesis** — All tones are generated in-browser via the Web
  Audio API; the Python backend is optional
- **Installable PWA** — Service worker caches the app for full offline use; "Add
  to Home Screen" on iOS/Android runs it fullscreen in airplane mode
- **Timeline Visualization** — Canvas-based animated timeline showing tone playback progress
- **Spectrum Analyzer** — Real-time FFT visualization using Web Audio API AnalyserNode
- **Preset & Macro Library** — Save/load/delete from `localStorage`, synced to
  the server when one is reachable
- **WAV Export** — Download generated sequences as WAV files
- **Themes** — Modern dark UI and a 1972 Blue Box skin (persisted in `localStorage`)
- **Mobile Responsive** — Works on phones and tablets

---

## 7. Core Engine Specification

### 7.1 Tone Generation

```python
import numpy as np

class ToneEngine:
    """Pure numpy-based tone synthesis."""
    
    # Bell System R1 MF frequencies
    MF_DIGITS = {
        "1": (700, 900), "2": (700, 1100), "3": (900, 1100),
        "4": (700, 1300), "5": (900, 1300), "6": (1100, 1300),
        "7": (700, 1500), "8": (900, 1500), "9": (1100, 1500),
        "0": (1300, 1500),
    }
    
    MF_SPECIAL = {
        "KP": (1100, 1700),
        "ST": (1500, 1700),
        "ST2": (900, 1700),
        "ST3": (1300, 1700),
    }
    
    SEIZURE_FREQ = 2600
    
    def generate_tone(self, frequencies, duration, sample_rate=8000, amplitude=0.7):
        """Generate tone samples."""
        num_samples = int(sample_rate * duration)
        t = np.linspace(0, duration, num_samples, endpoint=False)
        
        # Sum sine waves
        samples = sum(np.sin(2 * np.pi * freq * t) for freq in frequencies)
        samples = samples / len(frequencies) * amplitude
        
        # Apply fade in/out (5ms)
        fade_samples = int(sample_rate * 0.005)
        if fade_samples > 0 and num_samples > fade_samples * 2:
            fade_in = np.linspace(0, 1, fade_samples)
            fade_out = np.linspace(1, 0, fade_samples)
            samples[:fade_samples] *= fade_in
            samples[-fade_samples:] *= fade_out
        
        return samples.astype(np.float32)
    
    def generate_silence(self, duration, sample_rate=8000):
        """Generate silence."""
        return np.zeros(int(sample_rate * duration), dtype=np.float32)
    
    def build_sequence(self, digits, config):
        """Build complete MF sequence."""
        parts = []
        
        # Seizure tone
        parts.append(self.generate_tone(
            [self.SEIZURE_FREQ], config.seize_duration
        ))
        
        # Wink delay
        parts.append(self.generate_silence(config.wink_delay))
        
        # KP tone
        parts.append(self.generate_tone(
            self.MF_SPECIAL["KP"], config.kp_duration
        ))
        parts.append(self.generate_silence(config.inter_digit_gap))
        
        # Digits
        for i, digit in enumerate(digits):
            freq1, freq2 = self.MF_DIGITS[digit]
            parts.append(self.generate_tone(
                [freq1, freq2], config.digit_duration
            ))
            if i < len(digits) - 1:
                parts.append(self.generate_silence(config.inter_digit_gap))
        
        # ST tone
        parts.append(self.generate_silence(config.inter_digit_gap))
        parts.append(self.generate_tone(
            self.MF_SPECIAL["ST"], config.st_duration
        ))
        
        return np.concatenate(parts)
```

### 7.2 Audio Output

```python
class AudioOutput:
    """Unified audio output interface."""
    
    def __init__(self):
        self.backend = self._detect_backend()
    
    def _detect_backend(self):
        """Auto-detect best available audio backend."""
        try:
            import sounddevice as sd
            return "sounddevice"
        except ImportError:
            pass
        
        if self._has_aplay():
            return "aplay"
        
        if self._has_paplay():
            return "paplay"
        
        return "none"
    
    def play(self, samples, sample_rate=8000, device=None):
        """Play samples through audio device."""
        if self.backend == "sounddevice":
            import sounddevice as sd
            sd.play(samples, samplerate=sample_rate, device=device)
            sd.wait()
        elif self.backend == "aplay":
            self._play_aplay(samples, sample_rate, device)
        elif self.backend == "paplay":
            self._play_paplay(samples, sample_rate)
        else:
            raise RuntimeError("No audio backend available")
    
    def get_devices(self):
        """List available output devices."""
        if self.backend == "sounddevice":
            import sounddevice as sd
            return [
                {"index": i, "name": d["name"], "channels": d["max_output_channels"]}
                for i, d in enumerate(sd.query_devices())
                if d["max_output_channels"] > 0
            ]
        return []
```

### 7.3 FFT Verification

```python
class ToneVerifier:
    """Verify generated tones using FFT analysis."""
    
    def __init__(self):
        self.has_scipy = self._check_scipy()
    
    def verify_sequence(self, samples, sample_rate=8000):
        """Analyze sequence and report detected frequencies."""
        if self.has_scipy:
            from scipy import fft
            return self._verify_scipy(samples, sample_rate, fft)
        else:
            return self._verify_basic(samples, sample_rate)
    
    def _verify_scipy(self, samples, sample_rate, fft):
        """Use scipy FFT for accurate frequency detection."""
        # Window the samples into chunks
        chunk_size = int(sample_rate * 0.1)  # 100ms chunks
        results = []
        
        for i in range(0, len(samples), chunk_size):
            chunk = samples[i:i+chunk_size]
            if len(chunk) < chunk_size:
                continue
            
            # FFT
            freqs = fft.rfftfreq(chunk_size, 1/sample_rate)
            spectrum = np.abs(fft.rfft(chunk))
            
            # Find peaks
            peaks = self._find_peaks(freqs, spectrum, threshold=0.1)
            results.append({
                "time": i / sample_rate,
                "frequencies": peaks,
                "power": np.max(spectrum)
            })
        
        return results
    
    def _find_peaks(self, freqs, spectrum, threshold=0.1):
        """Find frequency peaks in spectrum."""
        max_power = np.max(spectrum)
        peaks = []
        
        for i in range(1, len(spectrum)-1):
            if spectrum[i] > spectrum[i-1] and spectrum[i] > spectrum[i+1]:
                if spectrum[i] > max_power * threshold:
                    peaks.append({
                        "frequency": round(freqs[i], 1),
                        "power": round(spectrum[i] / max_power, 3)
                    })
        
        return peaks
```

---

## 8. Preset System

### 8.1 Preset Format

```json
{
  "name": "projectmf-default",
  "description": "Standard ProjectMF dialing",
  "digits": "1234",
  "config": {
    "seize_duration": 2.0,
    "wink_delay": 0.5,
    "digit_duration": 0.06,
    "inter_digit_gap": 0.1,
    "kp_duration": 0.1,
    "st_duration": 0.1,
    "amplitude": 0.7,
    "sample_rate": 8000
  },
  "tags": ["projectmf", "default"],
  "created_at": "2026-05-15T10:00:00Z"
}
```

### 8.2 Built-in Presets

| Preset Name | Digits | Seize | Wink | Use Case |
|-------------|--------|-------|------|----------|
| `projectmf-default` | `1234` | 2.0s | 0.5s | Standard ProjectMF |
| `projectmf-slow` | `1234` | 3.0s | 1.0s | Fussy/older switches |
| `seize-only` | ` ` | 2.0s | 0.0s | Just seize trunk |
| `contest-day` | `8675309` | 2.5s | 0.8s | Conservative timing |
| `rapid-test` | `0` | 1.0s | 0.3s | Quick testing |

---

## 9. Configuration

### 9.1 Config File (`~/.softblue/config.yaml`)

```yaml
audio:
  default_device: null  # null = system default
  sample_rate: 8000
  amplitude: 0.7
  
generation:
  seize_duration: 2.0
  wink_delay: 0.5
  digit_duration: 0.06
  inter_digit_gap: 0.1
  kp_duration: 0.1
  st_duration: 0.1
  
ui:
  theme: phreakme  # phreakme, dark, light
  tui_refresh_rate: 30
  web_port: 8080
  web_host: 127.0.0.1
  
presets:
  auto_save: true
  default_directory: ~/.softblue/presets

logging:
  level: info
  file: ~/.softblue/log.txt
```

---

## 10. Error Handling

| Error | UI Message | Recovery |
|-------|-----------|----------|
| No audio device | "No audio output available" | Offer WAV export only |
| Invalid digit | `"X" is not a valid MF digit` | Highlight valid digits (0-9) |
| Clip/overload | "Amplitude too high — auto-normalizing" | Reduce amplitude, retry |
| Device busy | "Audio device in use" | Wait or select different device |
| Network error (web) | "Cannot start server — port in use" | Suggest different port |
| WebSocket disconnect | "Connection lost — reconnecting" | Auto-reconnect with backoff |

---

## 11. File Structure

```
softblue/
├── pyproject.toml              # Package config with optional deps
├── README.md
├── softblue/
│   ├── __init__.py
│   ├── __main__.py             # Entry point
│   ├── cli.py                  # CLI commands (click)
│   ├── tui.py                  # TUI app (textual)
│   ├── web.py                  # FastAPI server + static files
│   ├── engine.py               # Core tone generation
│   ├── audio.py                # Audio output backends
│   ├── verify.py               # FFT verification
│   ├── presets.py              # Preset management
│   ├── config.py               # Configuration loader
│   └── static/                 # Web frontend assets
│       ├── index.html
│       ├── style.css
│       ├── app.js
│       └── favicon.ico
├── tests/
│   ├── test_engine.py
│   ├── test_audio.py
│   ├── test_cli.py
│   └── test_web.py
└── docs/
    └── usage.md
```

---

## 12. pyproject.toml

```toml
[project]
name = "softblue"
version = "1.0.0"
description = "MF bluebox tone generator for telecom CTF prep"
requires-python = ">=3.10"
dependencies = [
    "numpy>=1.24",
    "click>=8.0",
]

[project.optional-dependencies]
cli = ["rich>=13.0"]
tui = ["textual>=0.50", "rich>=13.0"]
web = ["fastapi>=0.100", "uvicorn>=0.23", "websockets>=11.0", "jinja2>=3.1"]
audio = ["sounddevice>=0.4", "soundfile>=0.12"]
all = ["softblue[cli,tui,web,audio]"]

[project.scripts]
softblue = "softblue.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## 13. Quick Reference: MF Frequency Table

```
Digit │ Freq 1 │ Freq 2 │ softblue equivalent
──────┼────────┼────────┼────────────────────────────
  1   │  700   │  900   │ softblue generate 1
  2   │  700   │ 1100   │ softblue generate 2
  3   │  900   │ 1100   │ softblue generate 3
  4   │  700   │ 1300   │ softblue generate 4
  5   │  900   │ 1300   │ softblue generate 5
  6   │ 1100   │ 1300   │ softblue generate 6
  7   │  700   │ 1500   │ softblue generate 7
  8   │  900   │ 1500   │ softblue generate 8
  9   │ 1100   │ 1500   │ softblue generate 9
  0   │ 1300   │ 1500   │ softblue generate 0
──────┼────────┼────────┼────────────────────────────
 KP   │ 1100   │ 1700   │ auto-prepended
 ST   │ 1500   │ 1700   │ auto-appended
──────┼────────┼────────┼────────────────────────────
Seize │ 2600   │   —    │ auto-generated at start
```

---

## 14. Implementation Roadmap

### Phase 1: Core (2–3 hours)
- [ ] Tone engine (numpy synthesis)
- [ ] Sequence builder
- [ ] WAV export
- [ ] CLI with click
- [ ] Basic playback (aplay fallback)

### Phase 2: TUI (3–4 hours)
- [ ] Textual app framework
- [ ] Sequence builder screen
- [ ] Timeline visualization
- [ ] Device selection
- [ ] Preset management
- [ ] Real-time playback visualization

### Phase 3: Web (4–5 hours)
- [ ] FastAPI server
- [ ] WebSocket audio streaming
- [ ] Static frontend (HTML/CSS/JS)
- [ ] Spectrum analyzer (Web Audio API)
- [ ] Mobile responsive design

### Phase 4: Polish (2–3 hours)
- [ ] FFT verification
- [ ] Config file support
- [ ] Comprehensive tests
- [ ] Documentation

**Total estimate:** 11–15 hours for full implementation.

---

*SoftBlue Spec v1.0 — Ready for implementation. Feed this to OpenCode and start building! 🐚📞🔷*
