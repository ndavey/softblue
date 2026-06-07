"""SoftBlue command-line interface (click)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from .audio import AudioOutput, NoAudioBackendError
from .config import Config, Settings
from .engine import COIN_SCHEMES, GREEN_WINKS, MODES, InvalidDigitError, ToneEngine
from .macros import Macro, MacroError, MacroManager
from .presets import Preset, PresetError, PresetManager
from .verify import ToneVerifier

_MODE_CHOICE = click.Choice(MODES, case_sensitive=False)
_SCHEME_CHOICE = click.Choice(COIN_SCHEMES, case_sensitive=False)
_WINK_CHOICE = click.Choice(GREEN_WINKS, case_sensitive=False)

# Timing options shared by `generate` and `preset save`, each with a short alias.
_TIMING = [
    ("--seize-duration", "--seize", "seize_duration", float, "Seizure tone duration (s)"),
    ("--wink-delay", "--wink", "wink_delay", float, "Delay after seizure (s)"),
    ("--digit-duration", "--digit", "digit_duration", float, "MF digit duration (s)"),
    ("--inter-digit-gap", "--gap", "inter_digit_gap", float, "Gap between digits (s)"),
    ("--kp-duration", "--kp", "kp_duration", float, "KP tone duration (s)"),
    ("--st-duration", "--st", "st_duration", float, "ST tone duration (s)"),
]


def timing_options(f):
    for long, short, dest, typ, help_ in reversed(_TIMING):
        f = click.option(long, short, dest, type=typ, default=None, help=help_)(f)
    return f


def mode_options(f):
    f = click.option("--green-wink", type=_WINK_CHOICE, default=None,
                     help="Green-box operator-release wink (2600 | mf8)")(f)
    f = click.option("--coin-scheme", type=_SCHEME_CHOICE, default=None,
                     help="US red-box scheme (acts | phreakme)")(f)
    f = click.option("--mode", "-m", type=_MODE_CHOICE, default=None,
                     help=f"Signaling mode ({' | '.join(MODES)})")(f)
    return f


def _resolve_config(ctx, **overrides) -> Config:
    base: Config = ctx.obj["settings"].defaults
    g = ctx.obj.get("globals", {})
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
def generate(ctx, digits, output, seize_only, mode, coin_scheme, green_wink, **timing):
    """Generate a tone sequence and write it to a WAV file."""
    cfg = _resolve_config(ctx, seize_only=seize_only, mode=mode,
                          coin_scheme=coin_scheme, green_wink=green_wink, **timing)
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
         green_wink, **timing):
    """Generate and play a tone sequence through an audio device."""
    cfg = _resolve_config(ctx, seize_only=seize_only, mode=mode,
                          coin_scheme=coin_scheme, green_wink=green_wink, **timing)
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
                green_wink, **timing):
    cfg = _resolve_config(ctx, seize_only=seize_only, mode=mode,
                          coin_scheme=coin_scheme, green_wink=green_wink, **timing)
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
