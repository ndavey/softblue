# Red box: what happened this year, and what to do next year

Written 2026-08-09, right after DC34. Everything here was verified against
source at the time — `~/github/whopper` (the server) and `~/github/dc34-phreakme`
(the player-facing lab). Line references will rot; the reasoning won't.

---

## 1. Bottom line

**The coin tones never moved, and nothing was scoring them anyway.**

Two separate findings, and the second is the one that matters:

- `_redbox_segs` in `agi/tone_engine.py` was still nickel/dime = 1700 Hz at
  −6/−3 dBFS, quarter = 1700→2200, `$` = 1700+2200, on the same 60 ms grid.
  Exactly one commit has ever touched that function (`77e1f8a`, the original
  bluebox→whopper rebranding). softblue was sending the correct tones all along.

- `_check_coin_tones` (`agi/ctf_processor.agi`) is the only consumer of coin
  detection, and its own docstring says it **"does not trigger state-machine
  actions (purely informational)"**. It logs which of 1700/2200 was present so
  the UI timeline can show it, and stops. Every file in `challenges/` is
  voicemail. There is no coin, redbox or payphone extension anywhere in
  `asterisk-config/`.

So a null result this year carries **no information about the tones**. Do not
treat "it didn't work at DC34" as evidence that the table moved.

### Check these three before spending a single call minute

```bash
# 1. Is the coin path wired to scoring at all, or still just logging?
grep -n "_check_coin_tones" -A 12 ~/github/whopper/agi/ctf_processor.agi
grep -rln "coin\|redbox\|payphone" ~/github/whopper/challenges/ \
  ~/github/whopper/asterisk-config/

# 2. Did the table actually move?
grep -n "nickel\|dime\|quarter" ~/github/whopper/agi/tone_engine.py

# 3. Is the page you're holding current?
curl -s https://ndavey.github.io/softblue/sw.js | head -1
```

If (1) still says purely informational and there's no coin challenge, stop.
There is nothing to capture and no amount of sweeping will produce a flag.

---

## 2. The structural thesis (this is the durable part)

The whole coin table is generated from **one frequency pair**. 1700 carries
nickel, dime, collect and return; 2200 is the quarter's second symbol and the
dollar's partner. Everything else is structure:

| | ACTS | PhreakMe |
|---|---|---|
| carrier | 1700+2200 together | split into an ordered alphabet {A, B} |
| value | burst **count** | **level** (nickel/dime) and **order** (quarter) |
| nickel | 1 × 66 ms | A @ −6 dBFS |
| dime | 2 × 66 ms | A @ −3 dBFS |
| quarter | 5 × 33 ms | A then B |
| dollar | (none) | A + B simultaneous |
| grid | 66/33 ms | uniform 60 ms |

Last year's ACTS→PhreakMe change **was not really a frequency change**. ACTS
already used 1700+2200; the author re-roled them, decomposing a simultaneous
pair into an ordered two-symbol alphabet and re-encoding value in the degrees of
freedom that created — level, order, simultaneity — then flattening the burst
grid to a uniform 60/60.

**The grammar was the invention. You do not re-invent a grammar two years
running.** So "they changed the frequencies" almost certainly means the same
six-symbol grammar with the pair relocated.

### Why the search space is exactly 42

`detect_mf_samples` runs a Goertzel over exactly
`_MF_ALL_FREQS = [700, 900, 1100, 1300, 1500, 1700, 2200]`. A carrier outside
that list is invisible to the author's own decoder. 7 × 6 = 42 ordered pairs —
one evening of calls. Note **2600 is deliberately absent**, so no 2600-based
coin scheme is detectable.

### Hold these constant while sweeping

60 ms tone / 60 ms silence; `return` at 15 ms gaps; 2 ms raised-cosine fades;
8 kHz/16-bit/mono; nickel and dime the same tone separated only by level;
quarter an ordered sequence, not a burst count; collect = 3×A, return = 6×A;
`$` = A+B simultaneous. Per-component amplitude is `10^(L/20)/k`, so a
single-tone nickel at −6 dBFS is genuinely 6 dB down while each leg of a
two-tone `$` at −3 dBFS is about −9 dBFS. **Do not normalise this** — amplitude
is semantic, and rescaling silently converts a nickel into a dime.

---

## 3. What the tooling does now

Three commits this year: `bf91f6f` (Python core), `5bbdcad` (web UI),
`9a666a3` (docs).

### CLI

```bash
softblue redbox schemes -n 10          # ranked candidates, best first
softblue redbox schemes -n 3 --why     # full rationale per candidate
softblue redbox schemes --all-pairs    # ignore the ranking, all 42

softblue redbox sweep --via audio -n 8            # play into a handset, you judge
softblue redbox sweep 2195002600 --dial "w5 2 …"  # one call each, auto-scored
softblue redbox export /tmp/schemes -n 10         # just write WAVs

softblue redbox spec -f 1500,2200 -o hit.json     # pin a pair as a coin table
softblue play q -m phreakme_coin --coin-spec hit.json
```

The ranked list ships as package data (`softblue/data/redbox-candidates.json`)
and is the default order everywhere, followed by the remaining ordered pairs so
a sweep that exhausts the argued guesses keeps going rather than stopping at 18
of 42.

### Web UI — https://ndavey.github.io/softblue/

PhreakMe mode → **Coin scheme**. Candidate picker drives local Web Audio,
`/api/generate`, presets and macro recording alike, so what you audition is
always what gets sent. Candidates come from the server's ranked list when
reachable and a built-in enumeration when not, so it works on a phone with no
backend. **Custom…** takes an arbitrary pair, segment length, gap and levels.

**Sweep** walks the candidates playing the quarter, with hit/miss/skip persisted
to `localStorage` so a half-finished sweep survives a reload or a locked phone.

**Capture** records off the mic and reads a scheme back out — Goertzel over the
seven detectable frequencies, energy segmentation, then pattern inference
(quarter / dollar / collect / return). Verified against synthesised coins at
several SNRs: exact frequencies and durations at 60/66/100 ms, and it returns
*nothing* rather than a guess on noise, on silence, or when the room leaves too
little dynamic range to segment.

---

## 4. Order of operations at the con

**0. Try to record the real thing before predicting anything.** One clean
capture collapses the whole candidate list to a single answer and beats every
ranked guess. Two routes: the organisers hand out complimentary tone generation
devices at the payphone area (`phreakme_deep_dive.md:264-265`) — record one; or
provoke the challenge into emitting its own **collect** or **return**, which are
the loudest and most distinctive patterns in the scheme (3 pulses and 6).

**1. Dial the control first.** Establishes the baseline and tells you whether
anything moved at all.

**2. Probe with the QUARTER, never the nickel.** The quarter is the only symbol
that exercises A, B and their order in one burst, and it carries no level
semantics — so a miss is unambiguously a frequency miss. Nickel and dime differ
by 3 dB and nothing else; softblue's own acoustic model compresses that to about
0.75 dB through handset AGC. Any path gain error over 1.5 dB flips one into the
other. Where the challenge lets you choose a denomination for the actual solve,
prefer the quarter or `$`. Never stake a solve on 3 dB across an acoustic path.
(`redbox sweep` defaults to the quarter and warns if you override to n or d.)

**3. Run the 66 ms timing null early** despite its rank. It's cheap and
eliminates a whole branch.

### Diagnostic readings

| What you see | What it means | Where to go |
|---|---|---|
| Control credited, nothing else changes | nothing moved | level polarity, then timing |
| Coin accepted but **amount** wrong | level semantics moved, not frequencies | rank 18, then 10 |
| Nickel/dime credited, quarter not | only freq_b moved — "kept the workhorse, moved the marker" | ranks 9, 6 |
| Nothing credited at all | the workhorse moved | ranks 2, 4, 5, 7 (keep-2200-as-guard family) |
| Quarter accepted, nickel and dime both rejected **or both credited the same** | the three-symbol hypothesis — **stop sweeping pairs** | §5(A) |
| 2200-as-workhorse fails but 2200-as-marker partially responds | suspect the FXO path, not the scheme | bench-test the HT813 |

---

## 5. What a pair sweep structurally CANNOT express

`RedboxScheme` pins nickel, dime, collect and return to `freq_a` and uses
`freq_b` only for the quarter's second symbol and the dollar's partner. Three
plausible moves live outside that shape:

**(A) Three-symbol alphabet, level encoding retired** — e.g. nickel=900,
dime=1700, quarter=1700→2200. *This is arguably the strongest hypothesis
overall.* Level-only encoding is the one part of the current scheme the author's
**own receiver cannot read**: `detect_coin_samples` returns a bare
`(has_1700, has_2200)` tuple and nothing anywhere on the receive path measures
amplitude, so the server literally cannot tell a nickel from a dime today.
Giving each denomination its own frequency fixes that *and* satisfies "the
frequencies changed". If a coin-control triple is used, the closed canonical set
is the green box {700, 1100, 1700}.
**Signature: quarter accepted but nickel and dime both rejected, or both
credited as the same value.** If you see that, stop sweeping pairs and probe
bare single tones at each of the seven frequencies.

**(B) Burst count restored** on top of a new pair (nickel=1×A, dime=2×A) — a
partial return to ACTS.

**(C) Duration as the value encoder** (nickel=A@60ms, dime=A@120ms, same tone
and level).

**To cover these:** give `RedboxScheme` per-symbol overrides
(`freq_nickel`, `freq_dime`, `freq_quarter_1`, `freq_quarter_2`) defaulting to
A/A/A/B. The engine already supports it — `Config.coin_spec` and
`ToneEngine._build_phreakme_coin` take an arbitrary per-symbol segment list, so
only the search-space generator needs changing. The web UI's **Custom…** entry
and `--coin-spec` can already play any of these by hand today.

---

## 6. Known defects and risks

### Fix this first: softblue's MF special table disagrees with the server

Verified against `~/github/whopper/agi/tone_engine.py` `_MF_BELL`:

| | server | softblue `MF_SPECIAL` |
|---|---|---|
| KP | (1100, 1700) | (1100, 1700) ✓ |
| ST | (1500, 1700) | (1500, 1700) ✓ |
| KP2 | (1300, 1700) | — |
| ST3 | **(700, 1700)** | **(1300, 1700)** ✗ |
| ST2 | *does not exist* | (900, 1700) ✗ |

Consequences: softblue's "ST3" decodes server-side as **KP2**; softblue's "ST2"
is not in `_MF_DECODE` at all so `detect_mf_samples` returns `None`; and the
server's real ST3 (700+1700) is **unreachable from softblue**. Irrelevant to
coins — but it will bite on any blue box challenge. Same mismatch exists in
`MF_SPECIAL_COIN` and in `tone-engine.js`.

### Other risks

- **Out-of-alphabet wildcard.** `detect_coin_samples` hard-codes 1700.0 and
  2200.0, so *any* frequency move forces the author into that function. Once
  he's editing detector code, nothing stops him extending `_MF_ALL_FREQS` and
  putting the pair outside the R1 set. **2400 Hz** (CCITT #5 line signalling,
  documented but never used for coins) is the most likely addition. Rated
  unlikely — the design leans on 2200 being "the coin-identification guard" —
  but if all candidates miss, this is the next hypothesis, and blind capture is
  the only reliable answer to it.
- **Harmonic folding at FS=8000.** 2×1100 = 2200 lands exactly on the marker;
  2×1300 = 2600 lands exactly on `detect_2600_samples`. At −3 dBFS either victim
  detector trips once 2nd-harmonic distortion is worse than −32 dBc, which
  acoustic coupling exceeds trivially. Clean synthesised sines are fine; a
  speaker into a mouthpiece may not be.
- **HT813 FXO response at 2200 Hz is unverified** (`Tone Engine.md:171`, the
  author's own open item). Bench-test before blaming the scheme.
- **`$` vs coin KP.** The lab README and `Tone Engine.md` put CoinKP($) at
  **100 ms**, while `_redbox_segs` renders `$` at **60 ms**. Same frequencies,
  different signals — the spec flags the ambiguity itself. For the MF signalling
  entry use `--mf-variant coin --kp 0.1`, not the `$` coin key.

### Operational gotcha that cost real time

The PWA service worker is **cache-first**. After a push, the page serves the old
JS until the new worker installs and activates — one extra load. During
development it silently intercepts even `fetch(..., {cache: "reload"})`, so a
"verified" result can be from stale code. To test the real file: unregister the
worker and clear caches, or load the source into a fresh scope
(`new Function(src + ";return {…}")()`), or serve from a different port. Always
assert a sentinel string from the code under test before trusting a number.

---

## 7. Confidence posture

The ranked candidate list is **inference from design constraints and
demonstrated author taste — not a recovered secret**. Four independent analyses
searched git history, reflogs, stashes, dangling blobs, worktrees and challenge
JSON across all repos and found no new coin table anywhere. There is no prior
year-over-year coin-frequency change on record, so there is no observed
precedent to extrapolate from — only the ACTS→PhreakMe transformation described
in the docs.

Treat the top candidates as hypotheses to falsify fast, and treat one clean
recording as worth more than all of them.
