"""Terminal UI (textual). Reuses the shared engine / preset / audio layers.

Note: the spec's `R` (record from microphone) binding is intentionally omitted
— recording is unspecified and the engine has no capture path. Add it only
with a real spec.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from .audio import AudioOutput
from .config import Config, Settings
from .engine import InvalidDigitError, ToneEngine
from .presets import Preset, PresetError, PresetManager

_FIELDS = [
    ("seize_duration", "Seize"),
    ("wink_delay", "Wink"),
    ("digit_duration", "Digit"),
    ("inter_digit_gap", "Gap"),
    ("kp_duration", "KP"),
    ("st_duration", "ST"),
]


class SoftBlueTUI(App):
    CSS = """
    Screen { background: #0a0e27; }
    #builder { width: 50%; padding: 1 2; }
    #side { width: 50%; padding: 1 2; }
    Input { margin-bottom: 1; }
    #status { color: #00d4ff; height: 1; }
    Button { margin: 1 1 0 0; }
    ListView { height: 10; border: solid #2a3866; }
    """
    BINDINGS = [
        ("g", "generate", "Generate"),
        ("p", "play", "Play"),
        ("s", "save_preset", "Save"),
        ("w", "web", "Web info"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings, device=None):
        super().__init__()
        self.settings = settings
        self.device = device
        self.engine = ToneEngine()
        self.audio = AudioOutput()
        self.presets = PresetManager(settings.preset_dir)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        d = self.settings.defaults
        with Horizontal():
            with Vertical(id="builder"):
                yield Label("Digits")
                yield Input(value="8675309", id="digits")
                for key, lbl in _FIELDS:
                    yield Label(lbl)
                    yield Input(value=str(getattr(d, key)), id=key)
                yield Checkbox("Seize only", id="seize_only")
                yield Button("Generate WAV [G]", id="gen", variant="primary")
                yield Button("Play [P]", id="play")
            with Vertical(id="side"):
                yield Label(f"Device: {self.device or 'default'} ({self.audio.backend})")
                yield Label("Presets")
                yield ListView(id="presets")
                yield Button("Load preset", id="load")
                yield Button("Save preset [S]", id="save")
                yield Static("Ready", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_presets()

    def _refresh_presets(self) -> None:
        lv = self.query_one("#presets", ListView)
        lv.clear()
        for p in self.presets.list_all():
            lv.append(ListItem(Label(p["name"]), id=f"p_{p['name']}"))

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _config(self) -> Config:
        vals = {}
        for key, _ in _FIELDS:
            vals[key] = float(self.query_one(f"#{key}", Input).value)
        vals["seize_only"] = self.query_one("#seize_only", Checkbox).value
        cfg = self.settings.defaults.merged(**vals)
        cfg.validate()
        return cfg

    def _digits(self) -> str:
        return self.query_one("#digits", Input).value

    def action_generate(self) -> None:
        try:
            cfg = self._config()
            samples = self.engine.build_sequence(self._digits(), cfg)
            path = f"{self._digits() or 'seize'}.wav"
            self.engine.write_wav(path, samples, cfg.sample_rate)
            self._status(f"Wrote {path} ({len(samples) / cfg.sample_rate:.2f}s)")
        except (InvalidDigitError, ValueError) as e:
            self._status(f"Error: {e}")

    def action_play(self) -> None:
        if not self.audio.available:
            self._status("No audio backend — use Generate")
            return
        try:
            cfg = self._config()
            samples = self.engine.build_sequence(self._digits(), cfg)
            self._status("Playing…")
            self.audio.play(samples, cfg.sample_rate, self.device)
            self._status("Done")
        except (InvalidDigitError, ValueError, RuntimeError) as e:
            self._status(f"Error: {e}")

    def action_save_preset(self) -> None:
        try:
            name = self._digits() or "seize"
            self.presets.save(Preset(f"tui-{name}", self._digits(), self._config()))
            self._refresh_presets()
            self._status(f"Saved tui-{name}")
        except (PresetError, ValueError) as e:
            self._status(f"Error: {e}")

    def action_web(self) -> None:
        self._status(f"Run: softblue web  (default {self.settings.web_host}:{self.settings.web_port})")

    def on_list_view_selected(self, ev: ListView.Selected) -> None:
        name = ev.item.id.removeprefix("p_")
        try:
            p = self.presets.load(name)
        except PresetError as e:
            self._status(str(e))
            return
        self.query_one("#digits", Input).value = p.digits
        for key, _ in _FIELDS:
            self.query_one(f"#{key}", Input).value = str(getattr(p.config, key))
        self.query_one("#seize_only", Checkbox).value = p.config.seize_only
        self._status(f"Loaded {name}")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        {
            "gen": self.action_generate,
            "play": self.action_play,
            "save": self.action_save_preset,
            "load": lambda: self._status("Pick a preset from the list"),
        }.get(ev.button.id, lambda: None)()


def run_tui(settings: Settings, device=None) -> None:
    SoftBlueTUI(settings, device).run()
