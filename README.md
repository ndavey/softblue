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

## Probing an unknown coin detector

`softblue sweep` characterises a red-box challenge that won't respond, walking an
ordered grid of carrier/timing candidates and logging which one lands:

```bash
softblue sweep --coin 3 --log quarter-sweep.json
```

It also models the speaker → room → handset path and grades each candidate, so
you can tell "wrong tone" apart from "right tone, mangled on the way in" — see
[docs/usage.md](docs/usage.md#sweeping-a-black-box-coin-challenge).

## When the coin tones move

PhreakMe's whole coin table comes from one frequency pair, so if the organisers
change the frequencies, only that pair moves. `softblue redbox` enumerates that
space best-first (ranked analysis, then the remaining ordered pairs), tries
candidates over SIP or acoustically, and pins whichever one lands:

```bash
softblue redbox schemes -n 10                 # what to try, and why
softblue redbox sweep --via audio -n 8        # try them into a handset
softblue redbox spec -f 1500,2200 -o hit.json # pin the winner
softblue play q -m phreakme_coin --coin-spec hit.json
```

The same candidate list is in the web UI under **PhreakMe → Coin scheme**, with
a **Custom…** entry for a pair the list does not cover — see
[docs/usage.md](docs/usage.md#red-box-scheme-search-softblue-redbox).

Before running any of it at the next con, read
[docs/redbox-next-year.md](docs/redbox-next-year.md): what the DC34 server
actually did with coin tones, the three checks worth doing before spending call
minutes, and the hypotheses a frequency-pair sweep structurally cannot reach.

## Web app & offline PWA

`softblue web` serves the browser UI, but the front-end is **fully client-side**:
every tone (MF/R1, CCITT #5, DTMF, US/UK red box, 3-slot bell, green box, 2600 sweep) is
synthesized in-browser with the Web Audio API. The Python server is **optional**
— it only adds server-side audio output and shared preset/macro storage. Presets
and macros live in `localStorage`, so the app is fully functional with no
backend at all.

### Install on a phone (works offline)

The front-end is a PWA, so you can install it to a home screen and run it in
airplane mode:

1. Host the static folder [`softblue/static/`](softblue/static/) over **HTTPS**
   — this is required, iOS only registers a service worker on a secure origin
   (a plain `http://` LAN address will *not* cache offline). All asset paths are
   relative, so a sub-path deploy (`user.github.io/softblue/`) works. No build
   step — the folder *is* the app.
   - **GitHub Pages (included):** the
     [`Deploy PWA to GitHub Pages`](.github/workflows/pages.yml) workflow
     publishes `softblue/static/` on every push. One-time setup: repo
     **Settings → Pages → Source: GitHub Actions**. URL:
     `https://<user>.github.io/<repo>/`.
   - Or drag the folder onto Netlify Drop / connect Cloudflare Pages.
2. Open the HTTPS URL in **Safari** (iOS) or Chrome (Android) → **Add to Home Screen**.
3. **Launch it once from the home screen while online** so the service worker
   caches everything; afterward it runs fullscreen and offline.

> **iOS note:** WebKit purges a PWA's cache after ~7 days of non-use. Open the
> app at least once a week to keep it primed for offline launches. For
> eviction-proof permanent offline, wrap `softblue/static/` in a native shell
> (e.g. Capacitor) and sideload with an Apple Developer account.

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
