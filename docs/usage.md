# SoftBlue Usage

## Configuration precedence

Settings resolve as: **command-line flags > config file > built-in defaults**.

`~/.softblue/config.yaml` (override the base dir with `$SOFTBLUE_HOME`):

```yaml
audio:
  default_device: null     # null = system default
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
  theme: phreakme
  tui_refresh_rate: 30
  web_port: 8080
  web_host: 127.0.0.1
presets:
  default_directory: ~/.softblue/presets
logging:
  level: info
```

## CLI

```bash
softblue generate <digits> -o out.wav [--seize-only] [timing flags]
softblue play <digits> [--loop N] [--countdown N] [--device DEV]
softblue verify <file.wav>
softblue devices
softblue preset save <name> --digits 1234 --seize 2.5
softblue preset load <name> --play
softblue preset list | delete <name>
```

Digit strings accept `0-9` plus `-` and spaces as visual separators
(e.g. `555-1234`). Any other character is rejected with a clear message.

## Web app

The browser front-end synthesizes **all** tones client-side with the Web Audio
API and drives the live spectrum from a shared `AnalyserNode`. It is an
installable, offline-capable PWA (see the README's *Web app & offline PWA*
section for deploy + install steps) and does **not** require any of the
endpoints below — they back the CLI/TUI and optional server-side playback.
Presets and macros are stored in `localStorage` and synced to the server only
when one is reachable.

## Web API (optional backend)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/health` | status + audio backend |
| POST | `/api/generate` | `{digits, config}` → base64 WAV + duration |
| POST | `/api/play` | play on server device (409 if busy) |
| POST | `/api/verify` | per-100ms detected frequencies |
| GET  | `/api/devices` | output devices |
| GET/POST/DELETE | `/api/presets[/{name}]` | preset CRUD (name-sanitised) |
| GET/POST/DELETE | `/api/macros[/{name}]` | macro CRUD |
| POST | `/api/macros/{name}/play` \| `/render` | play / render a saved macro |
| WS   | `/ws/audio` | streams int16 LE PCM chunks |

## MF reference

| Digit | f1 | f2 | | Special | f1 | f2 |
|-------|----|----|-|---------|----|----|
| 1 | 700 | 900 | | KP | 1100 | 1700 |
| 2 | 700 | 1100 | | ST | 1500 | 1700 |
| 3 | 900 | 1100 | | ST2 | 900 | 1700 |
| 4 | 700 | 1300 | | ST3 | 1300 | 1700 |
| 5 | 900 | 1300 | | Seize | 2600 | — |
| 6 | 1100 | 1300 |
| 7 | 700 | 1500 |
| 8 | 900 | 1500 |
| 9 | 1100 | 1500 |
| 0 | 1300 | 1500 |

All tones stay below the 4 kHz Nyquist limit at the default 8 kHz rate.

## Green box (`-m green_box`)

Operator/TSPS coin-control signals sent by the **called** party over the voice
path of a connected fortress (payphone) call. Each symbol emits an operator
release "wink" followed by its control tone:

| Symbol | Function | Control tone | Duration |
|--------|----------|--------------|----------|
| `c` | coin collect | 700 + 1100 Hz | ~1 s |
| `r` | coin return  | 1100 + 1700 Hz | ~1 s |
| `b` | ringback     | 700 + 1700 Hz | ~2 s |

The wink is selectable with `--green-wink` (CLI) / the **Operator wink**
selector (web):

- `2600` *(default)* — a 2600 Hz operator-release signal: 90 ms on, 60 ms
  silence, 900 ms on.
- `mf8` — an MF "8" (900 + 1500 Hz) 90 ms burst followed by 60 ms silence.

```bash
softblue generate "crb" -m green_box -o coin-control.wav
softblue play "r" -m green_box --green-wink mf8
```

For authorized PhreakMe / ProjectMF lab use only.
