# SoftBlue

Multi-modal MF (multi-frequency) "bluebox" tone generator with **CLI**, **TUI**,
and self-hosted **web** interfaces. Built for PhreakMe CTF / ProjectMF lab prep.

> ⚠️ **Authorized lab use only.** SoftBlue generates the 2600 Hz seize tone and
> Bell System R1 MF tones used by the [ProjectMF](http://www.projectmf.org/) /
> PhreakMe hobbyist Asterisk environment, which emulates legacy phone switching.
> These tones have no effect on modern public telephone networks. Use only
> against systems you are authorized to test (your own ProjectMF lab / a CTF
> environment). The web server has **no authentication** — only bind it to a
> non-local interface (`--host 0.0.0.0`) deliberately on an isolated network.

## Install

```bash
pip install -e ".[all]"     # everything
pip install -e ".[audio]"   # CLI + live playback
pip install -e .            # CLI + WAV export only
```

## Quick start

```bash
softblue generate 8675309 -o call.wav      # write a WAV
softblue play 1234 --seize 3 --wink 1.0    # live playback (short or long flags)
softblue verify call.wav                   # FFT-analyse a WAV
softblue devices                           # list audio devices
softblue preset list                       # built-in + saved presets
softblue tui                               # interactive terminal UI
softblue web --open                        # browser interface
```

Long and short timing flags are interchangeable everywhere:
`--seize-duration`/`--seize`, `--wink-delay`/`--wink`, `--digit-duration`/`--digit`,
`--inter-digit-gap`/`--gap`, `--kp-duration`/`--kp`, `--st-duration`/`--st`.

`--seize-only` emits just the 2600 Hz trunk-seize tone (no KP/digits/ST).

## Config

Optional `~/.softblue/config.yaml` (see [docs/usage.md](docs/usage.md)). Set
`$SOFTBLUE_HOME` to relocate config + presets (used by the test suite).

## Layout

- `engine.py` — numpy tone synthesis, sequence builder, WAV I/O
- `audio.py` — sounddevice / aplay / paplay output
- `presets.py` — JSON preset store (name-sanitised)
- `verify.py` — numpy-FFT frequency verification
- `cli.py` / `tui.py` / `web.py` — the three front-ends

## Tests

```bash
pip install -e ".[dev]" && pytest
```
