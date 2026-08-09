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

## US red box (`-m us_redbox`)

Single-slot ACTS coin tones, sounded toward the CO to signal a deposit.
`1`=nickel, `2`=dime, `3`=quarter, `4`=dollar.

| Coin | Pulses | On | Gap |
|------|--------|----|-----|
| nickel  | 1 | 66 ms | — |
| dime    | 2 | 66 ms | 66 ms |
| quarter | 5 | 33 ms | 33 ms |

`4` (dollar) is **non-standard** — real ACTS has no dollar tone, and a target
asking for a dollar almost certainly wants four quarters.

`--coin-scheme` picks the carrier:

| Scheme | Carrier | Notes |
|--------|---------|-------|
| `acts` (default) | 1700 + 2200 Hz | Real Bell ACTS dual tone |
| `nortel` | 2200 Hz | Canadian / Nortel single tone |
| `phreakme` | 1700 Hz | Single 1700 Hz |

### Probing an unknown detector

When a target won't respond, `--coin-freqs`, `--coin-on` and `--coin-gap`
override the table above so a candidate can be replayed without code edits:

```bash
softblue play 3 -m us_redbox --coin-freqs 2200 --coin-on 0.05 --coin-gap 0.05
```

`softblue redbox sweep` drives that search automatically — see
[Sweeping a black-box coin challenge](#sweeping-a-black-box-coin-challenge).

## Red box scheme search (`softblue redbox`)

PhreakMe's coin table is generated from a single frequency pair — 1700 Hz
carries nickel, dime, collect and return; 2200 Hz is the quarter's second
symbol and the dollar's partner. Everything else is structure. So when the
organisers change the frequencies, the structure survives and only the pair
moves, which turns an open-ended hunt into an ordered sweep over the
frequencies their own detector can measure.

```bash
# What to try, and why — the shipped ranking, best-first
softblue redbox schemes -n 10
softblue redbox schemes -n 3 --why      # full rationale per candidate
```

The default list is the ranked analysis shipped in
`softblue/data/redbox-candidates.json`, followed by every remaining ordered
pair. `--all-pairs` skips the ranking and enumerates the bare 42; `--ranked
FILE` supplies your own. Candidate 1 is always last year's exact table, so a
sweep has a baseline before it calls anything a hit.

Candidates differ on more than frequency — a restored 66 ms segment, an
inverted nickel/dime split — so each one is named for every axis it moves
(`1700->2200 66ms`, `1700->2200 n-3/d-6`). That name is the sweep's log key and
its resume key.

### Trying them

```bash
# Over SIP — one call per scheme, response compared against a no-coin control
softblue redbox sweep 2195002600 --dial "w5 2 w2 212-555-1337" -n 8 --log rb.json

# Out the sound device — you judge each one; no PBX needed
softblue redbox sweep --via audio -n 8 --log rb.json

# Neither: just write WAVs
softblue redbox export /tmp/schemes -n 10 --symbols ndq$
```

The mode is inferred from whether you give an EXTENSION; `--via` forces it.

In audio mode, **set the output level once and leave it alone** — level is
semantic in this scheme (nickel and dime are the same tone 3 dB apart), so
changing the volume mid-sweep changes which coin you are sending.

### Pinning a scheme once you have it

`redbox spec` renders any pair as a full six-symbol coin table, so a scheme you
just discovered is playable without a code edit:

```bash
# From a pair you worked out (e.g. from `softblue analyze` on a recording)
softblue redbox spec -f 1500,2200 -o hit.json

# Or by index into the candidate list
softblue redbox spec -s 2 -o hit.json

softblue play q -m phreakme_coin --coin-spec hit.json
softblue sip call 5551212 --digits q -m phreakme_coin --coin-spec hit.json
```

### From the web UI

`softblue web` → **PhreakMe** mode shows the same candidate list under **Coin
scheme**, and plays the selected one straight out of the browser — which is the
practical rig at the con: phone speaker against the handset mouthpiece. The
candidates are also built into the page, so the picker still works with the
backend unreachable. **Custom…** takes an arbitrary A/B pair, segment length and
nickel/dime levels for anything the list does not cover.

## Sweeping a black-box coin challenge

`softblue sweep` walks an ordered grid of (carrier, on-time, gap) candidates,
plays each, and records whether it landed. Results are logged to JSON and the
sweep resumes from that log if interrupted.

```bash
softblue sweep --coin 3 --log quarter-sweep.json
```

Useful options:

| Option | Purpose |
|--------|---------|
| `--dry-run` | Print the plan without playing anything |
| `--amplitudes 0.5,0.7,0.9` | Multiply the grid across output levels |
| `--wet` | Reverb mix of the acoustic model (see below) |
| `--skip-doomed` | Drop candidates the acoustic path cannot deliver |
| `--countdown N` | Seconds before each probe, to position a handset |

### The acoustic model

Playing tones from a speaker into a handset mouthpiece is a lossy path: room
reverb fills the gaps *between* coin pulses, and once a detector can no longer
see those gaps it reads one long tone instead of a pulse train. `sweep` models
speaker → room → handset mic and grades each candidate before you waste a call
on it.

`--wet` is the reverb mix, and it dominates everything else:

| `--wet` | Setup | Gap depth (33 ms quarter) |
|---------|-------|---------------------------|
| 0.05 | Speaker pressed to the mouthpiece | −39 dB |
| 0.15 | Speaker a few cm away | −28 dB |
| 0.30 | Handset on a desk nearby | −20 dB |
| 0.45 | Across a small room | −13 dB — marginal |
| 0.60 | Across a hard-walled room | unresolvable |

A detector needs roughly −12 dB of gap depth to resolve pulses, so **coupling
tightness matters far more than timing**: at reasonable coupling the canonical
33 ms quarter is fine, and at loose coupling *no* timing survives. Press the
speaker against the mouthpiece before you start changing frequencies.

## Calling the PBX directly (`softblue sip`)

Injects tones straight into the RTP stream, and records the far end. This is
strictly better than playing into a handset: acoustic coupling destroys the two
things the PhreakMe coin scheme depends on — absolute level (nickel and dime
differ only by 3 dB) and clean tone edges.

### Credentials

The password is read only from `$SOFTBLUE_SIP_PASSWORD` or `~/.softblue/sip.yaml`
— never from a flag, which would leak it into shell history and the process
table.

```yaml
# ~/.softblue/sip.yaml
host: pbx.example.lan
user: softphone
password: "..."
register: true
```

### Dialing

```bash
softblue sip call 5551212 --digits q -m phreakme_coin --listen 5 --analyze
```

| Option | Purpose |
|--------|---------|
| `--digits` / `-m` | Generate tones and play them into the call |
| `--play FILE.wav` | Play an existing 8 kHz WAV |
| `--listen N` | Seconds to hold the call and record |
| `--record OUT.wav` | Save the far-end audio |
| `--analyze` | Blind-scan the recording for a coin scheme |
| `--no-register` | For a PBX that identifies by IP instead |

Everything received is kept, including the challenge's own prompt tones — those
are usually the most informative thing on the call, and `analyze` timestamps
each segment so prompt and response stay distinguishable.

### Constraints

Only G.711 µ-law/A-law over UDP, which is exactly what the lab PBX offers
(`disallow=all` / `allow=ulaw` / `allow=alaw`). That is fortunate rather than
limiting: G.711 is plain 8-bit companding with no frame model, so a 60 ms coin
tone passes through intact. Anything transform-based (Opus, GSM, G.729) would
round the tone edges the detector keys on.

Audio must be generated at 8 kHz (`-r 8000`, the default). The transport refuses
other rates rather than resampling, since resampling would soften those edges.

No TLS, SRTP, TCP, or re-INVITE handling.

## 3-slot bell (`-m bell_3slot`)

Western Electric 3-slot payphone gong/bell tones, sounded as the **caller**
deposits coins. Each uses a struck-bell (exponential-decay) envelope:

| Symbol | Coin | Tone |
|--------|------|------|
| `1` | nickel  | one 1664 Hz ding |
| `2` | dime    | two 1664 Hz dings |
| `3` | quarter | one 800 Hz gong |

```bash
softblue generate "123" -m bell_3slot -o coins.wav
```

(The single-tone 1-slot ACTS coin tones are produced by `-m us_redbox`.)

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
