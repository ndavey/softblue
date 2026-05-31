# SoftBlue

Multi-modal MF (multi-frequency) "bluebox" tone generator with **CLI**, **TUI**,
and an installable, offline-capable **web app (PWA)**. Built for PhreakMe CTF /
ProjectMF lab prep.

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

## Web app & offline PWA

`softblue web` serves the browser UI, but the front-end is **fully client-side**:
every tone (MF/R1, CCITT #5, DTMF, US/UK red box, coin, 2600 sweep) is
synthesized in-browser with the Web Audio API. The Python server is **optional**
— it only adds server-side audio output and shared preset/macro storage. Presets
and macros live in `localStorage`, so the app is fully functional with no
backend at all.

### Install on a phone (works offline)

The front-end is a PWA, so you can install it to a home screen and run it in
airplane mode:

1. Deploy the static folder [`softblue/static/`](softblue/static/) to any HTTPS
   static host (Cloudflare Pages, Netlify, GitHub Pages). All asset paths are
   relative, so a sub-path deploy (`user.github.io/softblue/`) works too. No
   build step — the folder *is* the app.
2. Open the URL in **Safari** (iOS) or Chrome (Android) → **Add to Home Screen**.
3. Launch it once while online so the service worker caches everything;
   afterward it runs fullscreen and offline.

The service worker ([`sw.js`](softblue/static/sw.js)) version-stamps its cache
and fetches with `cache: "reload"` on install. When you change a static asset,
bump `CACHE` in `sw.js` **and** the `?v=` query on the `style.css` / `*.js`
references in [`index.html`](softblue/static/index.html) so clients pick it up.

### Themes

A toggle in the header switches between the default **Modern** dark UI and a
**1972 Blue Box** skin — brushed blue anodized panel, silver pushbuttons, amber
KP/ST keys, and a red LED readout. The choice persists in `localStorage` and is
applied before first paint (no flash).

## Config

Optional `~/.softblue/config.yaml` (see [docs/usage.md](docs/usage.md)). Set
`$SOFTBLUE_HOME` to relocate config + presets (used by the test suite).

## Layout

- `engine.py` — numpy tone synthesis, sequence builder, WAV I/O
- `audio.py` — sounddevice / aplay / paplay output
- `presets.py` — JSON preset store (name-sanitised)
- `verify.py` — numpy-FFT frequency verification
- `cli.py` / `tui.py` / `web.py` — the three front-ends
- `static/` — the PWA: client-side tone engine (`tone-engine.js`), UI (`app.js`,
  `style.css`), service worker (`sw.js`), and web app manifest

## Tests

```bash
pip install -e ".[dev]" && pytest
```
