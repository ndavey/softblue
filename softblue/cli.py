"""SoftBlue command-line interface (click)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click
import yaml

from .audio import AudioOutput, NoAudioBackendError
from .config import Config, Settings
from .engine import (
    COIN_SCHEMES,
    GREEN_WINKS,
    MF_VARIANTS,
    MODES,
    InvalidDigitError,
    ToneEngine,
)
from .macros import Macro, MacroError, MacroManager
from .presets import Preset, PresetError, PresetManager
from .verify import ToneVerifier

_MODE_CHOICE = click.Choice(MODES, case_sensitive=False)
_SCHEME_CHOICE = click.Choice(COIN_SCHEMES, case_sensitive=False)
_WINK_CHOICE = click.Choice(GREEN_WINKS, case_sensitive=False)
_VARIANT_CHOICE = click.Choice(MF_VARIANTS, case_sensitive=False)

# Timing options shared by `generate` and `preset save`, each with a short alias.
_TIMING = [
    ("--seize-duration", "--seize", "seize_duration", float, "Seizure tone duration (s)"),
    ("--wink-delay", "--wink", "wink_delay", float, "Delay after seizure (s)"),
    ("--digit-duration", "--digit", "digit_duration", float, "MF digit duration (s)"),
    ("--inter-digit-gap", "--gap", "inter_digit_gap", float, "Gap between digits (s)"),
    ("--kp-duration", "--kp", "kp_duration", float, "KP tone duration (s)"),
    ("--st-duration", "--st", "st_duration", float, "ST tone duration (s)"),
    # Red-box probe overrides — let a swept result be replayed without code edits.
    ("--coin-on", "--con", "coin_on", float, "Red-box burst on-time override (s)"),
    ("--coin-gap", "--cgap", "coin_gap", float, "Red-box inter-burst gap override (s)"),
    ("--coin-freqs", "--cfreq", "coin_freqs", str,
     "Red-box carrier override, e.g. '2200' or '1700,2200'"),
    ("--coin-spec", "--cspec", "coin_spec", str,
     "PhreakMe coin-spec JSON from `softblue analyze`"),
]


def timing_options(f):
    for long, short, dest, typ, help_ in reversed(_TIMING):
        f = click.option(long, short, dest, type=typ, default=None, help=help_)(f)
    return f


def mode_options(f):
    # The coin KP/ST table (KP 1700+2200 / ST 1500+2200) is the PhreakMe payphone
    # signalling path, and at kp_duration it is a *different* signal from the 60 ms
    # '$' coin symbol despite sharing frequencies — the server's own spec flags
    # that ambiguity. The web UI has always been able to pick it; the CLI could
    # only reach it through a config file.
    f = click.option("--mf-variant", type=_VARIANT_CHOICE, default=None,
                     help=f"MF KP/ST table ({' | '.join(MF_VARIANTS)})")(f)
    f = click.option("--green-wink", type=_WINK_CHOICE, default=None,
                     help="Green-box operator-release wink (2600 | mf8)")(f)
    f = click.option("--coin-scheme", type=_SCHEME_CHOICE, default=None,
                     help=f"US red-box scheme ({' | '.join(COIN_SCHEMES)})")(f)
    f = click.option("--mode", "-m", type=_MODE_CHOICE, default=None,
                     help=f"Signaling mode ({' | '.join(MODES)})")(f)
    return f


def _resolve_config(ctx, **overrides) -> Config:
    base: Config = ctx.obj["settings"].defaults
    g = ctx.obj.get("globals", {})
    raw = overrides.get("coin_freqs")
    if isinstance(raw, str):
        try:
            overrides["coin_freqs"] = [float(f) for f in raw.split(",") if f.strip()]
        except ValueError:
            raise click.ClickException(f"could not parse --coin-freqs {raw!r}")
    spec = overrides.get("coin_spec")
    if isinstance(spec, str):
        import json as _json
        try:
            overrides["coin_spec"] = _json.loads(Path(spec).read_text())
        except (OSError, ValueError) as e:
            raise click.ClickException(f"could not read --coin-spec {spec!r}: {e}")
    return base.merged(
        sample_rate=g.get("sample_rate"),
        amplitude=g.get("amplitude"),
        **overrides,
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--device", "-d", default=None, help="Audio device index or name")
@click.option("--sample-rate", "-r", type=int, default=None, help="Sample rate")
@click.option("--amplitude", "-a", type=float, default=None, help="Amplitude 0.0-1.0")
@click.option("--config", "-c", "config_path", type=click.Path(), default=None)
@click.option("--verbose", "-v", is_flag=True)
@click.version_option(package_name="softblue")
@click.pass_context
def cli(ctx, device, sample_rate, amplitude, config_path, verbose):
    """SoftBlue — MF bluebox tone generator for PhreakMe / ProjectMF lab use."""
    ctx.ensure_object(dict)
    settings = Settings.load(Path(config_path) if config_path else None)
    ctx.obj["settings"] = settings
    ctx.obj["verbose"] = verbose
    ctx.obj["globals"] = {
        "device": device if device is not None else settings.default_device,
        "sample_rate": sample_rate,
        "amplitude": amplitude,
    }


@cli.command()
@click.argument("digits")
@click.option("--output", "-o", required=True, type=click.Path(), help="Output WAV file")
@click.option("--seize-only", is_flag=True, default=None, help="Seize trunk only (mf/c5)")
@mode_options
@timing_options
@click.pass_context
def generate(ctx, digits, output, seize_only, mode, coin_scheme, green_wink,
             mf_variant, **timing):
    """Generate a tone sequence and write it to a WAV file."""
    cfg = _resolve_config(ctx, seize_only=seize_only, mode=mode,
                          coin_scheme=coin_scheme, green_wink=green_wink,
                          mf_variant=mf_variant, **timing)
    try:
        cfg.validate()
        samples = ToneEngine().build_sequence(digits, cfg)
    except (InvalidDigitError, ValueError) as e:
        raise click.ClickException(str(e))
    ToneEngine().write_wav(output, samples, cfg.sample_rate)
    click.echo(f"Wrote {output} ({len(samples) / cfg.sample_rate:.2f}s)")


@cli.command()
@click.argument("digits")
@click.option("--device", default=None, help="Audio device")
@click.option("--loop", "-l", type=int, default=1, help="Loop playback N times")
@click.option("--countdown", "-n", type=int, default=0, help="Countdown before playing")
@click.option("--seize-only", is_flag=True, default=None)
@mode_options
@timing_options
@click.pass_context
def play(ctx, digits, device, loop, countdown, seize_only, mode, coin_scheme,
         green_wink, mf_variant, **timing):
    """Generate and play a tone sequence through an audio device."""
    cfg = _resolve_config(ctx, seize_only=seize_only, mode=mode,
                          coin_scheme=coin_scheme, green_wink=green_wink,
                          mf_variant=mf_variant, **timing)
    try:
        cfg.validate()
        samples = ToneEngine().build_sequence(digits, cfg)
    except (InvalidDigitError, ValueError) as e:
        raise click.ClickException(str(e))
    out = AudioOutput()
    if not out.available:
        raise click.ClickException("No audio output available — use `generate` for WAV.")
    for s in range(countdown, 0, -1):
        click.echo(f"  {s}...")
        time.sleep(1)
    dev = device or ctx.obj["globals"]["device"]
    for n in range(max(1, loop)):
        if loop > 1:
            click.echo(f"Playing ({n + 1}/{loop})...")
        try:
            out.play(samples, cfg.sample_rate, dev)
        except (NoAudioBackendError, RuntimeError) as e:
            raise click.ClickException(str(e))


@cli.command()
@click.pass_context
def devices(ctx):
    """List available audio output devices."""
    out = AudioOutput()
    click.echo(f"Backend: {out.backend}")
    for d in out.get_devices():
        click.echo(f"  [{d['index']}] {d['name']} ({d['channels']} ch)")
    if not out.available:
        click.echo("  (none — WAV export only)")


@cli.command()
@click.argument("wav_file", type=click.Path(exists=True))
@click.pass_context
def verify(ctx, wav_file):
    """Analyse a WAV file and report detected MF frequencies."""
    samples, sr = ToneEngine().read_wav(wav_file)
    results = ToneVerifier().verify_sequence(samples, sr)
    for r in results:
        if r["silent"]:
            continue
        fr = ", ".join(f"{p['frequency']}Hz" for p in r["frequencies"])
        click.echo(f"  t={r['time']:>6.3f}s  {fr}")


@cli.command()
@click.argument("wav_file", type=click.Path(exists=True))
@click.option("--symbol", "-s", default=None,
              help="Save the scanned pattern under this coin symbol (e.g. q)")
@click.option("--json", "json_path", type=click.Path(), default=None,
              help="Write/merge a --coin-spec file (requires --symbol)")
@click.option("--silence-db", type=float, default=-45.0,
              help="Gate below the peak that counts as silence")
@click.option("--min-ms", type=float, default=12.0, help="Ignore runs shorter than this")
def analyze(wav_file, symbol, json_path, silence_db, min_ms):
    """Recover a coin scheme from a recording, assuming nothing about it.

    Reports every tone/silence run with its frequencies, duration and level, so
    a scheme that has been changed can be read straight off the wire instead of
    guessed at. With --symbol/--json the result is written as a spec file that
    `play`/`generate` accept via --coin-spec.
    """
    import json as _json

    from .sweep import scan_segments, spec_from_segments

    samples, sr = ToneEngine().read_wav(wav_file)
    segs = scan_segments(samples, sr, silence_db=silence_db, min_ms=min_ms)
    if not segs:
        raise click.ClickException("No segments found — check the gate (--silence-db).")

    click.echo(f"{wav_file}  ({len(samples) / sr:.3f}s @ {sr}Hz)\n")
    for s in segs:
        click.echo(s.describe())

    tones = [s for s in segs if not s.silent]
    if tones:
        levels = [s.level_dbfs for s in tones]
        click.echo(f"\n{len(tones)} tone segment(s); "
                   f"level spread {max(levels) - min(levels):.1f} dB")
        if max(levels) - min(levels) > 1.5:
            click.echo("  note: levels differ materially — in the PhreakMe scheme "
                       "level is semantic (nickel vs dime), so preserve it.")

    spec = spec_from_segments(segs)
    click.echo(f"\nspec: {_json.dumps(spec)}")
    if json_path:
        if not symbol:
            raise click.ClickException("--json needs --symbol (e.g. --symbol q)")
        p = Path(json_path)
        existing = {}
        if p.exists():
            try:
                existing = _json.loads(p.read_text())
            except ValueError:
                raise click.ClickException(f"{json_path} is not valid JSON")
        existing[symbol] = spec
        p.write_text(_json.dumps(existing, indent=2))
        click.echo(f"\nWrote {json_path} (symbol {symbol!r}). Play it with:")
        click.echo(f"  softblue play {symbol} -m phreakme_coin --coin-spec {json_path}")


@cli.command()
@click.option("--scheme", type=click.Choice(["acts", "phreakme"]), default="acts",
              help="Shape of the search space: Bell ACTS burst counts, or "
                   "PhreakMe's tone-sequence coins")
@click.option("--symbol", default="q",
              help="PhreakMe coin symbol to probe (n d q $ c r)")
@click.option("--freqs", "freq_pool", default=None,
              help="PhreakMe frequency pool, e.g. '1700,2200,1500,1300'")
@click.option("--durations", default=None,
              help="PhreakMe tone durations in seconds, e.g. '0.06,0.08'")
@click.option("--single-tone", is_flag=True,
              help="PhreakMe: probe one tone per coin instead of an ordered pair")
@click.option("--spec-out", type=click.Path(), default=None,
              help="Write hits as a --coin-spec file")
@click.option("--coin", type=click.Choice(["1", "2", "3", "4"]), default="3",
              help="Coin to probe: 1=nickel 2=dime 3=quarter 4=dollar")
@click.option("--amplitudes", default="0.7",
              help="Comma-separated amplitudes to try (e.g. 0.5,0.7,0.9)")
@click.option("--repeat", type=int, default=1, help="Plays per probe")
@click.option("--countdown", "-n", type=int, default=3,
              help="Seconds before each probe, to get the handset in position")
@click.option("--acoustic/--no-acoustic", default=True,
              help="Grade each probe against the speaker-into-handset model")
@click.option("--wet", type=float, default=0.35,
              help="Reverb mix: 0.05 = speaker on the mouthpiece, 0.5 = across a room")
@click.option("--rt60", type=float, default=0.35, help="Room 60dB decay time (s)")
@click.option("--skip-doomed", is_flag=True,
              help="Drop probes the acoustic model says cannot survive your path")
@click.option("--log", "log_path", type=click.Path(), default=None,
              help="JSON results log (resumes from it if it exists)")
@click.option("--dry-run", is_flag=True, help="List the plan without playing")
@click.option("--device", default=None, help="Audio device")
@click.pass_context
def sweep(ctx, scheme, symbol, freq_pool, durations, single_tone, spec_out,
          coin, amplitudes, repeat, countdown, acoustic, wet, rt60,
          skip_doomed, log_path, dry_run, device):
    """Probe a black-box coin challenge across the frequency/timing grid.

    Plays each candidate in turn and records whether it landed, so an unknown
    detector can be characterised without guessing. Results are logged to JSON
    and the sweep resumes from that log if interrupted.
    """
    import json as _json

    from .sweep import (
        PHREAKME_DURATIONS,
        PHREAKME_FREQ_POOL,
        AcousticPath,
        candidates,
        candidates_phreakme,
        survivable,
    )

    def _floats(raw, name):
        try:
            vals = tuple(float(v) for v in raw.split(",") if v.strip())
        except ValueError:
            raise click.ClickException(f"could not parse {name} {raw!r}")
        if not vals:
            raise click.ClickException(f"{name} needs at least one value")
        return vals

    amps = _floats(amplitudes, "--amplitudes")

    if scheme == "phreakme":
        base = _resolve_config(ctx, mode="phreakme_coin")
        probes = candidates_phreakme(
            symbol=symbol,
            freqs=_floats(freq_pool, "--freqs") if freq_pool else PHREAKME_FREQ_POOL,
            durations=(_floats(durations, "--durations") if durations
                       else PHREAKME_DURATIONS),
            two_tone=not single_tone,
        )
        # Level is semantic in this scheme, so the acoustic burst-count model
        # (built around ACTS pulse trains) does not apply here.
        acoustic = False
    else:
        base = _resolve_config(ctx, mode="us_redbox")
        probes = candidates(coin=coin, amplitudes=amps)
    try:
        base.validate()
    except ValueError as e:
        raise click.ClickException(str(e))

    path = AcousticPath(rt60=rt60, wet=wet)

    graded: list[tuple[object, object]] = []
    for p in probes:
        graded.append((p, survivable(p, base, path) if acoustic else None))
    if skip_doomed and acoustic:
        kept = [(p, r) for p, r in graded if r.intact]
        dropped = len(graded) - len(kept)
        graded = kept
        click.echo(f"Dropped {dropped} probe(s) the acoustic path cannot deliver.")

    done: dict[str, str] = {}
    lp = Path(log_path) if log_path else None
    if lp and lp.exists():
        try:
            done = {e["label"]: e["result"] for e in _json.loads(lp.read_text())}
            click.echo(f"Resuming — {len(done)} probe(s) already recorded.")
        except (ValueError, KeyError, OSError):
            click.echo("Could not read log; starting fresh.", err=True)

    target = f"symbol {symbol!r}" if scheme == "phreakme" else f"coin {coin!r}"
    click.echo(f"\n{len(graded)} probe(s) for {target} ({scheme}), "
               f"most-likely first.\n")
    if dry_run:
        for i, (p, r) in enumerate(graded, 1):
            grade = f"  [{r.verdict}]" if r else ""
            click.echo(f"  {i:>3}. {p.timing_label:<16} {p.label}{grade}")
        return

    out = AudioOutput()
    if not out.available:
        raise click.ClickException("No audio output available — use --dry-run.")
    dev = device or ctx.obj["globals"]["device"]

    results: list[dict] = [{"label": k, "result": v} for k, v in done.items()]

    def _flush():
        if lp:
            lp.write_text(_json.dumps(results, indent=2))

    hits = []
    for i, (p, r) in enumerate(graded, 1):
        if p.label in done:
            continue
        click.echo(f"Probe {i}/{len(graded)} — {p.timing_label}")
        click.echo(f"  {p.label}")
        if r and not r.intact:
            click.echo(f"  acoustic: {r.verdict} — "
                       f"{r.bursts}/{r.expected} pulses, gap {r.gap_depth_db:.1f}dB")
        samples = p.render(base)
        while True:
            ch = click.prompt("  [enter]=play  y=hit  n=miss  s=skip  q=quit",
                              default="", show_default=False)
            ch = ch.strip().lower()
            if ch == "":
                for s in range(countdown, 0, -1):
                    click.echo(f"    {s}...")
                    time.sleep(1)
                for _ in range(max(1, repeat)):
                    try:
                        out.play(samples, base.sample_rate, dev)
                    except (NoAudioBackendError, RuntimeError) as e:
                        raise click.ClickException(str(e))
                continue
            if ch in ("y", "n", "s", "q"):
                break
        if ch == "q":
            _flush()
            click.echo("Stopped.")
            break
        results.append({"label": p.label, "result": ch, **p.to_dict()})
        if ch == "y":
            hits.append(p)
            click.echo("  ✓ recorded as a hit")
        _flush()

    if hits:
        click.echo("\nHits:")
        for p in hits:
            click.echo(f"  {p.label}")
        h = hits[0]
        if scheme == "phreakme":
            out = Path(spec_out or "phreakme-hit.json")
            out.write_text(_json.dumps({h.symbol: h.segments}, indent=2))
            click.echo(f"\nWrote {out}. Reproduce the first hit with:")
            click.echo(f"  softblue play {h.symbol} -m phreakme_coin --coin-spec {out}")
        else:
            click.echo("\nReproduce the first hit with:")
            click.echo(f"  softblue play {h.coin} -m us_redbox "
                       f"--coin-scheme {h.scheme} --coin-on {h.on_s} "
                       f"--coin-gap {h.gap_s} -a {h.amplitude}")
    elif lp:
        click.echo(f"\nNo hits recorded. Log: {lp}")


def _sip_account(host, port, user, no_register=False):
    """Thin wrapper over sipcall.load_account that reports as a CLI error."""
    from .sipcall import SipError, load_account

    try:
        return load_account(host=host, port=port, user=user,
                            no_register=no_register)
    except SipError as e:
        raise click.ClickException(str(e))


def _sip_options(f):
    f = click.option("--no-register", is_flag=True,
                     help="Skip REGISTER (for a PBX that identifies by IP)")(f)
    f = click.option("--user", default=None, help="SIP username")(f)
    f = click.option("--port", type=int, default=None, help="SIP port")(f)
    f = click.option("--host", default=None, help="PBX host")(f)
    return f


@cli.group()
def sip():
    """Place calls to the PBX and inject tones directly into the RTP stream.

    Credentials come from $SOFTBLUE_SIP_PASSWORD or ~/.softblue/sip.yaml —
    never from a flag.
    """


@sip.command("call")
@click.argument("extension")
@_sip_options
@click.option("--dial", "dial_str", default=None,
              help="Dial string, e.g. '2;212-555-1337;w5[q]' "
                   "(digits=DTMF, ','=0.5s, ';'=2s, w<sec>, [coins])")
@click.option("--digits", default=None, help="Generate these digits and play them")
@click.option("--play", "play_wav", type=click.Path(exists=True), default=None,
              help="Play an existing 8kHz WAV into the call")
@click.option("--listen", type=float, default=5.0,
              help="Seconds to record the far end after dialing")
@click.option("--record", "record_wav", type=click.Path(), default=None,
              help="Write the far-end audio to a WAV")
@click.option("--analyze", "do_analyze", is_flag=True,
              help="Scan the recorded audio for a coin scheme")
@click.option("--wait-before", type=float, default=1.0,
              help="Seconds to wait after answer before playing")
@click.option("--timeout", type=float, default=30.0, help="SIP timeout (s)")
@mode_options
@timing_options
@click.pass_context
def sip_call(ctx, extension, host, port, user, no_register, dial_str, digits, play_wav,
             listen, record_wav, do_analyze, wait_before, timeout,
             mode, coin_scheme, green_wink, **timing):
    """Dial EXTENSION, optionally play tones, and record what comes back."""
    from .dialstring import DialStringError, describe
    from .dialstring import parse as parse_dial
    from .sipcall import RTP_SAMPLE_RATE, SipCall, SipError
    from .sweep import scan_segments

    acct = _sip_account(host, port, user, no_register=no_register)

    steps = []
    if dial_str:
        try:
            steps = parse_dial(dial_str)
        except DialStringError as e:
            raise click.ClickException(str(e))
        click.echo(f"Dial string: {describe(steps)}")

    coin_cfg = _resolve_config(ctx, mode="phreakme_coin").merged(
        sample_rate=RTP_SAMPLE_RATE)

    def _render_coins(symbols):
        return ToneEngine().build_sequence(symbols, coin_cfg)
    samples = None
    if digits:
        cfg = _resolve_config(ctx, mode=mode, coin_scheme=coin_scheme,
                              green_wink=green_wink, **timing)
        cfg = cfg.merged(sample_rate=RTP_SAMPLE_RATE)
        try:
            cfg.validate()
            samples = ToneEngine().build_sequence(digits, cfg)
        except (InvalidDigitError, ValueError) as e:
            raise click.ClickException(str(e))
    elif play_wav:
        samples, sr = ToneEngine().read_wav(play_wav)
        if sr != RTP_SAMPLE_RATE:
            raise click.ClickException(
                f"{play_wav} is {sr}Hz; G.711 needs {RTP_SAMPLE_RATE}Hz. "
                f"Regenerate with -r {RTP_SAMPLE_RATE}.")

    call = SipCall(acct, timeout=timeout)
    try:
        with call:
            click.echo(f"Dialing {extension}@{acct.host} as {acct.user}...")
            call.dial(extension)
            from .g711 import CODECS
            click.echo(f"  connected — codec {CODECS[call.rtp.payload_type][0]}, "
                       f"rtp {call.rtp.remote[0]}:{call.rtp.remote[1]}")
            if wait_before > 0:
                call.listen(wait_before)
            if steps:
                call.run_steps(
                    steps, _render_coins,
                    on_step=lambda e: click.echo(
                        f"  [{e['at']:>6.2f}s] {e['detail']}"))
            # Nothing is cleared: the challenge's own prompt tones are usually
            # the most informative thing on the call, and `analyze` timestamps
            # each segment so prompt and response stay distinguishable anyway.
            if samples is not None:
                click.echo(f"  playing {len(samples) / RTP_SAMPLE_RATE:.2f}s")
                call.play(samples)
            click.echo(f"  listening {listen:.1f}s")
            heard = call.listen(listen)
    except SipError as e:
        raise click.ClickException(str(e))

    if not len(heard):
        click.echo("\nNo audio received. If the PBX is behind NAT, check that "
                   "it learned our RTP address (comedia).")
        return
    click.echo(f"\nReceived {len(heard) / RTP_SAMPLE_RATE:.2f}s of far-end audio.")
    if record_wav:
        ToneEngine().write_wav(record_wav, heard, RTP_SAMPLE_RATE)
        click.echo(f"Wrote {record_wav}")
    if do_analyze:
        segs = scan_segments(heard, RTP_SAMPLE_RATE)
        tones = [s for s in segs if not s.silent]
        click.echo(f"\n{len(tones)} tone segment(s):")
        for s in segs:
            click.echo(s.describe())


@cli.group()
def redbox():
    """Search for PhreakMe's current coin scheme.

    Their whole coin table is generated from one frequency pair, so when the
    organisers change the frequencies the structure survives and only the pair
    moves. These commands enumerate that space and test it over a live call.
    """


def _load_schemes(ranked=None, all_pairs=False, durations=None):
    """Resolve the candidate list every `redbox` subcommand works from.

    Default is the shipped ranking (then the un-argued pairs); ``--all-pairs``
    forces the bare enumeration, which is what you want once the ranked guesses
    are spent or when sweeping a non-default segment length.
    """
    from .redbox import MF_ALPHABET, candidates, default_candidates, load_ranked

    if all_pairs or durations:
        try:
            durs = tuple(float(d) for d in (durations or "0.060").split(",")
                         if d.strip())
        except ValueError:
            raise click.ClickException(f"could not parse --durations {durations!r}")
        return candidates(durations=durs)
    if ranked:
        schemes = load_ranked(ranked)
        if not schemes:
            raise click.ClickException(
                f"{ranked} held no candidates inside the detector's alphabet "
                f"{MF_ALPHABET}")
        return schemes
    return default_candidates()


def _ranked_options(f):
    f = click.option("--durations", default=None,
                     help="Comma-separated segment durations (s); implies --all-pairs")(f)
    f = click.option("--all-pairs", is_flag=True,
                     help="Ignore the shipped ranking; enumerate every ordered pair")(f)
    f = click.option("--ranked", type=click.Path(exists=True), default=None,
                     help="Ranked candidate JSON to use instead of the shipped one")(f)
    return f


@redbox.command("schemes")
@click.option("--limit", "-n", type=int, default=15, help="How many to show")
@click.option("--why", is_flag=True, help="Print each candidate's full rationale")
@_ranked_options
def redbox_schemes(limit, why, ranked, all_pairs, durations):
    """List candidate coin schemes, most-likely first."""
    from .redbox import MF_ALPHABET

    schemes = _load_schemes(ranked, all_pairs, durations)
    click.echo(f"{len(schemes)} candidate(s); alphabet = {MF_ALPHABET}\n")
    for i, s in enumerate(schemes[:limit], 1):
        click.echo(f"  {i:>3}. {s.describe()}")
        if s.rationale:
            click.echo(f"       {s.rationale if why else s.rationale[:96]}")
    if len(schemes) > limit:
        click.echo(f"\n  ... {len(schemes) - limit} more (raise -n to see them)")


@redbox.command("spec")
@click.option("--freqs", "-f", default=None,
              help="Ad-hoc pair 'A,B' in Hz — e.g. what `softblue analyze` just found")
@click.option("--scheme", "-s", "index", type=int, default=None,
              help="1-based index into the candidate list instead of --freqs")
@click.option("--duration", type=float, default=None, help="Segment length (s)")
@click.option("--gap", type=float, default=None, help="Inter-segment gap (s)")
@click.option("--nickel-dbfs", type=float, default=None, help="Nickel level (dBFS)")
@click.option("--dime-dbfs", type=float, default=None, help="Dime level (dBFS)")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write the coin-spec JSON here (default: stdout)")
@_ranked_options
def redbox_spec(freqs, index, duration, gap, nickel_dbfs, dime_dbfs, output,
                ranked, all_pairs, durations):
    """Emit one scheme as a coin-spec JSON, ready for `--coin-spec`.

    This is the bridge from "the tones moved and I worked out what to" back to
    playing them: `softblue analyze` tells you the pair, this renders the whole
    six-symbol table, and `softblue play q -m phreakme_coin --coin-spec …` sends
    it. No code edit in between.
    """
    import json as _json

    from .redbox import CONTROL_DIME_DBFS, CONTROL_NICKEL_DBFS, RedboxScheme

    if freqs and index:
        raise click.ClickException("use --freqs or --scheme, not both")
    if freqs:
        try:
            a, b = (float(x) for x in freqs.split(","))
        except ValueError:
            raise click.ClickException(
                f"could not parse --freqs {freqs!r} — expected 'A,B' in Hz")
        scheme = RedboxScheme(freq_a=a, freq_b=b)
    else:
        schemes = _load_schemes(ranked, all_pairs, durations)
        i = index or 1
        if not 1 <= i <= len(schemes):
            raise click.ClickException(f"--scheme must be 1-{len(schemes)}")
        scheme = schemes[i - 1]

    if duration is not None:
        scheme.duration = duration
    if gap is not None:
        scheme.gap = gap
    scheme.nickel_dbfs = (nickel_dbfs if nickel_dbfs is not None
                          else CONTROL_NICKEL_DBFS if freqs else scheme.nickel_dbfs)
    scheme.dime_dbfs = (dime_dbfs if dime_dbfs is not None
                        else CONTROL_DIME_DBFS if freqs else scheme.dime_dbfs)

    text = _json.dumps(scheme.coin_spec(), indent=2)
    if output:
        Path(output).write_text(text)
        click.echo(f"Wrote {output}  ({scheme.describe()})")
        click.echo(f"  softblue play q -m phreakme_coin --coin-spec {output}")
    else:
        click.echo(text)


@redbox.command("export")
@click.argument("outdir", type=click.Path())
@click.option("--symbols", default="ndq$", help="Coin symbols to render per scheme")
@click.option("--limit", "-n", type=int, default=10, help="How many schemes")
@_ranked_options
@click.pass_context
def redbox_export(ctx, outdir, symbols, limit, ranked, all_pairs, durations):
    """Render each candidate scheme to WAVs, for offline or manual testing."""
    import json as _json

    schemes = _load_schemes(ranked, all_pairs, durations)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    base = _resolve_config(ctx, mode="phreakme_coin").merged(sample_rate=8000)
    index = []
    for i, s in enumerate(schemes[:limit], 1):
        cfg = base.merged(coin_spec=s.coin_spec())
        for sym in symbols:
            samples = ToneEngine().build_sequence(sym, cfg)
            name = f"{i:02d}_{s.freq_a:.0f}-{s.freq_b:.0f}_{sym.replace('$','dollar')}.wav"
            ToneEngine().write_wav(str(out / name), samples, cfg.sample_rate)
        index.append({"n": i, **s.to_dict()})
    (out / "schemes.json").write_text(_json.dumps(index, indent=2))
    click.echo(f"Wrote {len(index)} scheme(s) x {len(symbols)} symbol(s) to {out}")


@redbox.command("sweep")
@click.argument("extension", required=False)
@click.option("--via", type=click.Choice(["sip", "audio"]), default=None,
              help="Delivery: 'sip' dials EXTENSION, 'audio' plays each scheme "
                   "out the sound device. Inferred from EXTENSION if omitted.")
@click.option("--device", "audio_device", default=None,
              help="Audio device (--via audio)")
@click.option("--repeat", type=int, default=1, help="Plays per scheme (--via audio)")
@click.option("--countdown", type=int, default=3,
              help="Seconds before each play, to position a handset (--via audio)")
@_sip_options
@click.option("--dial", "dial_str", default=None,
              help="Dial string to reach the coin prompt, e.g. 'w11 2 w4 212-555-1337'")
@click.option("--symbol", default="q",
              help="Coin to test per scheme. Keep the default: the quarter is "
                   "the only symbol that exercises A, B and their order in one "
                   "burst and carries no level semantics, so a miss is "
                   "unambiguously a frequency miss")
@click.option("--limit", "-n", type=int, default=8, help="Schemes to try")
@_ranked_options
@click.option("--listen", type=float, default=8.0, help="Seconds to record after the coin")
@click.option("--settle", type=float, default=6.0,
              help="Seconds between calls, so the PBX is not hammered")
@click.option("--threshold", type=float, default=0.25,
              help="Response-difference distance that counts as a hit")
@click.option("--log", "log_path", type=click.Path(), default=None,
              help="JSON results log (resumes from it)")
@click.option("--dry-run", is_flag=True, help="Show the plan without calling")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def redbox_sweep(ctx, extension, via, audio_device, repeat, countdown,
                 host, port, user, no_register, dial_str, symbol,
                 limit, ranked, all_pairs, durations, listen, settle, threshold,
                 log_path, dry_run, yes):
    """Try each candidate scheme on a live call and report which one lands.

    Each scheme costs one real call: dial EXTENSION, walk --dial to reach the
    coin prompt, send the coin, then record the reply. The first call is a
    control that sends no coin, establishing what "not accepted" sounds like;
    every later response is compared against it.
    """
    import json as _json
    import time as _time

    from .dialstring import DialStringError
    from .dialstring import parse as parse_dial
    from .redbox import fingerprint, response_changed
    from .sipcall import RTP_SAMPLE_RATE, SipCall, SipError, SipSession

    schemes = _load_schemes(ranked, all_pairs, durations)[:limit]
    if not schemes:
        raise click.ClickException("no candidate schemes to try")

    # Sweeping on the nickel or the dime confounds two independent dimensions:
    # those two are the same tone 3 dB apart, so a miss cannot distinguish
    # "wrong frequency" from "level didn't survive the path". Worse, the level
    # dimension may not be measurable at all — the server's coin detector
    # returns presence only. The quarter tests A, B and their order at once and
    # carries no level semantics, which is why it is the default.
    if symbol in ("n", "d"):
        click.echo(
            f"  ! Sweeping on {symbol!r} confounds frequency with level — the two\n"
            f"    differ by nothing but 3 dB, and a handset's AGC compresses that\n"
            f"    to well under 1 dB. A miss will not tell you which dimension\n"
            f"    failed. Sweep with --symbol q first; only split nickel from dime\n"
            f"    once a quarter is being accepted.", err=True)

    via = via or ("sip" if extension else "audio")
    if via == "sip" and not extension:
        raise click.ClickException(
            "--via sip needs an EXTENSION to dial (or use --via audio)")

    steps = []
    if dial_str:
        try:
            steps = parse_dial(dial_str)
        except DialStringError as e:
            raise click.ClickException(str(e))

    if via == "audio":
        _redbox_sweep_audio(schemes, symbol, ctx, audio_device, repeat,
                            countdown, log_path, dry_run, yes)
        return

    click.echo(f"{len(schemes)} scheme(s), one call each, coin {symbol!r}:\n")
    for i, s in enumerate(schemes, 1):
        click.echo(f"  {i:>3}. {s.describe()}")
    click.echo(f"\nEach call: dial {extension}"
               + (f" -> {dial_str}" if dial_str else "")
               + f" -> coin -> listen {listen:g}s;  {settle:g}s between calls.")
    est = len(schemes) * (listen + settle + 12)
    click.echo(f"Rough total: {est / 60:.1f} min, {len(schemes) + 1} calls "
               f"(the first is a no-coin control).")
    if dry_run:
        return
    if not yes:
        click.confirm("\nPlace these calls?", abort=True)

    acct = _sip_account(host, port, user, no_register=no_register)
    base = _resolve_config(ctx, mode="phreakme_coin").merged(
        sample_rate=RTP_SAMPLE_RATE)

    lp = Path(log_path) if log_path else None
    results: list[dict] = []
    if lp and lp.exists():
        try:
            results = _json.loads(lp.read_text())
            click.echo(f"Resuming — {len(results)} result(s) already logged.")
        except ValueError:
            click.echo("Could not read log; starting fresh.", err=True)
    done = {r["scheme"] for r in results}

    # One registration for the whole sweep. Registering per call churns the
    # PBX's AOR (each REGISTER evicts the previous contact) and leaves the
    # qualify OPTIONS unanswered between calls.
    sweep_session = SipSession(acct, timeout=30.0)
    if acct.register:
        try:
            sweep_session.register()
        except SipError as e:
            sweep_session.close()
            raise click.ClickException(f"registration failed: {e}")

    def _one_call(scheme):
        """Place a call, optionally send the coin, return the far-end audio."""
        call = SipCall(acct, timeout=30.0, session=sweep_session)
        with call:
            call.dial(extension)
            if steps:
                call.run_steps(steps, lambda sym: ToneEngine().build_sequence(
                    sym, base.merged(coin_spec=scheme.coin_spec())
                    if scheme else base))
            if scheme is not None:
                call.play(ToneEngine().build_sequence(
                    symbol, base.merged(coin_spec=scheme.coin_spec())))
            return call.listen(listen)

    try:
        click.echo("\nControl call (no coin) — establishing the baseline...")
        baseline = fingerprint(_one_call(None), RTP_SAMPLE_RATE)
        click.echo(f"  baseline: {baseline.duration:.1f}s audio, "
                   f"speech {baseline.speech_ratio * 100:.0f}%")
    except SipError as e:
        sweep_session.close()
        raise click.ClickException(f"control call failed: {e}")

    hits = []
    for i, s in enumerate(schemes, 1):
        if s.name in done:
            continue
        click.echo(f"\n[{i}/{len(schemes)}] {s.describe()}")
        _time.sleep(settle)
        try:
            fp = fingerprint(_one_call(s), RTP_SAMPLE_RATE)
        except SipError as e:
            click.echo(f"  call failed: {e}")
            results.append({"scheme": s.name, "error": str(e), **s.to_dict()})
            if lp:
                lp.write_text(_json.dumps(results, indent=2))
            continue
        changed, dist = response_changed(baseline, fp, threshold)
        click.echo(f"  response distance {dist:.3f}"
                   + ("   *** DIFFERENT — candidate hit ***" if changed else ""))
        results.append({"scheme": s.name, "distance": round(dist, 4),
                        "changed": changed, "fingerprint": fp.to_dict(),
                        **s.to_dict()})
        if changed:
            hits.append((s, dist))
        if lp:
            lp.write_text(_json.dumps(results, indent=2))

    sweep_session.close()

    click.echo("\n" + "=" * 60)
    if hits:
        hits.sort(key=lambda h: -h[1])
        click.echo("Schemes that changed the far-end response:")
        for s, d in hits:
            click.echo(f"  {d:.3f}  {s.describe()}")
        best = hits[0][0]
        click.echo(f"\nPin the strongest ({best.name}) and replay it with:")
        click.echo(f"  softblue redbox spec -f {best.freq_a:.0f},{best.freq_b:.0f} "
                   f"-o hit.json")
        click.echo(f"  softblue sip call {extension} --dial '{dial_str or ''}' "
                   f"--digits {symbol} -m phreakme_coin --coin-spec hit.json")
    else:
        click.echo("No scheme changed the response. Consider: widening --limit, "
                   "checking that --dial actually reaches the coin prompt, or "
                   "lowering --threshold.")
    if lp:
        click.echo(f"Log: {lp}")


def _redbox_sweep_audio(schemes, symbol, ctx, device, repeat, countdown,
                        log_path, dry_run, yes):
    """Play each candidate scheme out the sound device, you judge the result.

    The same candidate grid as the SIP sweep, delivered acoustically so it can be
    tested against a handset or a real payphone without depending on the trunk.
    Levels are preserved exactly — the PhreakMe coins encode value as level, so
    set the output gain once and leave it alone between schemes.
    """
    import json as _json

    click.echo(f"{len(schemes)} scheme(s), coin {symbol!r}, played locally:\n")
    for i, sc in enumerate(schemes, 1):
        click.echo(f"  {i:>3}. {sc.describe()}")
    if dry_run:
        return

    out = AudioOutput()
    if not out.available:
        raise click.ClickException(
            "No audio output available. Install the [audio] extra, or use "
            "`softblue redbox export <dir>` to write WAVs instead.")
    click.echo(f"\nOutput: {out.backend}. Level is semantic in this scheme — "
               "set the volume once and do not change it mid-sweep.")
    if not yes:
        click.confirm("\nStart?", abort=True)

    base = _resolve_config(ctx, mode="phreakme_coin").merged(sample_rate=8000)
    dev = device or ctx.obj["globals"]["device"]

    lp = Path(log_path) if log_path else None
    results: list[dict] = []
    done: set[str] = set()
    if lp and lp.exists():
        try:
            results = _json.loads(lp.read_text())
            done = {r["scheme"] for r in results}
            click.echo(f"Resuming — {len(done)} scheme(s) already recorded.")
        except (ValueError, KeyError):
            click.echo("Could not read log; starting fresh.", err=True)

    hits = []
    for i, sc in enumerate(schemes, 1):
        if sc.name in done:
            continue
        click.echo(f"\n[{i}/{len(schemes)}] {sc.describe()}")
        samples = ToneEngine().build_sequence(
            symbol, base.merged(coin_spec=sc.coin_spec()))
        while True:
            ch = click.prompt("  [enter]=play  y=hit  n=miss  s=skip  q=quit",
                              default="", show_default=False).strip().lower()
            if ch == "":
                for n in range(countdown, 0, -1):
                    click.echo(f"    {n}...")
                    time.sleep(1)
                for _ in range(max(1, repeat)):
                    try:
                        out.play(samples, base.sample_rate, dev)
                    except (NoAudioBackendError, RuntimeError) as e:
                        raise click.ClickException(str(e))
                continue
            if ch in ("y", "n", "s", "q"):
                break
        if ch == "q":
            break
        results.append({"scheme": sc.name, "result": ch, "via": "audio",
                        **sc.to_dict()})
        if ch == "y":
            hits.append(sc)
            click.echo("  ✓ recorded as a hit")
        if lp:
            lp.write_text(_json.dumps(results, indent=2))

    click.echo("\n" + "=" * 60)
    if hits:
        click.echo("Schemes you marked as hits:")
        for sc in hits:
            click.echo(f"  {sc.describe()}")
        click.echo("\nRender the first to WAV with:")
        click.echo(f"  softblue redbox export /tmp/hit -n 1")
    else:
        click.echo("No hits recorded.")
    if lp:
        click.echo(f"Log: {lp}")


@cli.group()
def preset():
    """Manage presets."""


@preset.command("list")
@click.pass_context
def preset_list(ctx):
    mgr = PresetManager(ctx.obj["settings"].preset_dir)
    for p in mgr.list_all():
        click.echo(f"  {p['name']:<22} {p.get('description', '')}")


@preset.command("save")
@click.argument("name")
@click.option("--digits", default="", help="Digits to dial")
@click.option("--description", default="", help="Preset description")
@click.option("--seize-only", is_flag=True, default=None)
@mode_options
@timing_options
@click.pass_context
def preset_save(ctx, name, digits, description, seize_only, mode, coin_scheme,
                green_wink, mf_variant, **timing):
    cfg = _resolve_config(ctx, seize_only=seize_only, mode=mode,
                          coin_scheme=coin_scheme, green_wink=green_wink,
                          mf_variant=mf_variant, **timing)
    mgr = PresetManager(ctx.obj["settings"].preset_dir)
    try:
        mgr.save(Preset(name=name, digits=digits, config=cfg, description=description))
    except PresetError as e:
        raise click.ClickException(str(e))
    click.echo(f"Saved preset {name!r}")


@preset.command("load")
@click.argument("name")
@click.option("--play", "do_play", is_flag=True, help="Play after loading")
@click.pass_context
def preset_load(ctx, name, do_play):
    mgr = PresetManager(ctx.obj["settings"].preset_dir)
    try:
        p = mgr.load(name)
    except PresetError as e:
        raise click.ClickException(str(e))
    click.echo(f"{p.name}: digits={p.digits!r} {p.config.to_dict()}")
    if do_play:
        samples = ToneEngine().build_sequence(p.digits, p.config)
        out = AudioOutput()
        if not out.available:
            raise click.ClickException("No audio output available.")
        out.play(samples, p.config.sample_rate, ctx.obj["globals"]["device"])


@preset.command("delete")
@click.argument("name")
@click.pass_context
def preset_delete(ctx, name):
    mgr = PresetManager(ctx.obj["settings"].preset_dir)
    try:
        mgr.delete(name)
    except PresetError as e:
        raise click.ClickException(str(e))
    click.echo(f"Deleted preset {name!r}")


@cli.group()
def macro():
    """Manage macros — chained tone-sequence steps."""


@macro.command("list")
@click.pass_context
def macro_list(ctx):
    mgr = MacroManager()
    for m in mgr.list_all():
        pin = "★ " if m.get("pinned") else "  "
        click.echo(f"  {pin}{m['name']:<22} {m.get('description','')}  "
                   f"({len(m.get('steps', []))} steps)")


@macro.command("show")
@click.argument("name")
def macro_show(name):
    try:
        m = MacroManager().load(name)
    except MacroError as e:
        raise click.ClickException(str(e))
    import json as _json
    click.echo(_json.dumps(m.to_dict(), indent=2))


@macro.command("save")
@click.argument("name")
@click.argument("json_file", type=click.Path(exists=True))
@click.option("--description", default="")
@click.option("--pin/--no-pin", default=False)
def macro_save(name, json_file, description, pin):
    """Save a macro from a JSON file containing a ``steps`` list."""
    import json as _json
    raw = _json.loads(Path(json_file).read_text())
    steps = raw["steps"] if isinstance(raw, dict) and "steps" in raw else raw
    try:
        MacroManager().save(Macro(name, steps, description, pin))
    except MacroError as e:
        raise click.ClickException(str(e))
    click.echo(f"Saved macro {name!r}")


@macro.command("delete")
@click.argument("name")
def macro_delete(name):
    try:
        MacroManager().delete(name)
    except MacroError as e:
        raise click.ClickException(str(e))
    click.echo(f"Deleted macro {name!r}")


@macro.command("run")
@click.argument("name")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write to WAV instead of playing")
@click.pass_context
def macro_run(ctx, name, output):
    """Play (or render) a macro through the audio device or to a WAV file."""
    mgr = MacroManager()
    pmgr = PresetManager(ctx.obj["settings"].preset_dir)
    try:
        m = mgr.load(name)
        samples = ToneEngine().build_macro(
            m.steps, ctx.obj["settings"].defaults, pmgr.load)
    except (MacroError, PresetError, InvalidDigitError, ValueError) as e:
        raise click.ClickException(str(e))
    sr = ctx.obj["settings"].defaults.sample_rate
    if output:
        ToneEngine().write_wav(output, samples, sr)
        click.echo(f"Wrote {output} ({len(samples) / sr:.2f}s)")
        return
    out = AudioOutput()
    if not out.available:
        raise click.ClickException("No audio output available — use --output WAV.")
    try:
        out.play(samples, sr, ctx.obj["globals"]["device"])
    except (NoAudioBackendError, RuntimeError) as e:
        raise click.ClickException(str(e))


@cli.command()
@click.option("--device", "-d", default=None)
@click.pass_context
def tui(ctx, device):
    """Launch the interactive TUI."""
    try:
        from .tui import run_tui
    except ImportError:
        raise click.ClickException("TUI deps missing — install softblue[tui]")
    run_tui(ctx.obj["settings"], device or ctx.obj["globals"]["device"])


@cli.command()
@click.option("--port", type=int, default=None)
@click.option("--host", default=None)
@click.option("--open", "open_browser", is_flag=True, help="Open browser on start")
@click.pass_context
def web(ctx, port, host, open_browser):
    """Start the self-hosted web interface."""
    try:
        from .web import run_web
    except ImportError:
        raise click.ClickException("Web deps missing — install softblue[web]")
    s = ctx.obj["settings"]
    run_web(s, host or s.web_host, port or s.web_port, open_browser)


if __name__ == "__main__":
    cli()
