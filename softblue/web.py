"""Self-hosted web interface (FastAPI + WebSocket streaming).

Security note: this server exposes audio playback control with no auth. It is
intended only for the isolated PhreakMe / ProjectMF lab. Binding to 0.0.0.0
makes it reachable on the local network — do so deliberately.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio import AudioOutput
from .config import Config, Settings
from .engine import InvalidDigitError, ToneEngine
from .macros import Macro, MacroError, MacroManager
from .presets import Preset, PresetError, PresetManager
from .verify import ToneVerifier

STATIC_DIR = Path(__file__).parent / "static"


class GenerateRequest(BaseModel):
    digits: str = ""
    config: dict = Field(default_factory=dict)


class PlayRequest(GenerateRequest):
    device: str | int | None = None


class PresetModel(BaseModel):
    name: str
    digits: str = ""
    description: str = ""
    config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class MacroModel(BaseModel):
    name: str
    description: str = ""
    steps: list[dict] = Field(default_factory=list)
    pinned: bool = False


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="SoftBlue Web", version="1.0.0")
    engine = ToneEngine()
    audio = AudioOutput()
    presets = PresetManager(settings.preset_dir)
    macros = MacroManager()
    play_lock = threading.Lock()

    def _cfg(data: dict) -> Config:
        c = settings.defaults.merged(**(data or {}))
        c.validate()
        return c

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "1.0.0", "audio": audio.backend}

    @app.post("/api/generate")
    def generate(req: GenerateRequest):
        try:
            cfg = _cfg(req.config)
            samples = engine.build_sequence(req.digits, cfg)
        except (InvalidDigitError, ValueError) as e:
            raise HTTPException(400, str(e))
        wav = engine.to_wav_bytes(samples, cfg.sample_rate)
        return {
            "audio": base64.b64encode(wav).decode("ascii"),
            "duration": len(samples) / cfg.sample_rate,
            "sample_rate": cfg.sample_rate,
        }

    @app.post("/api/verify")
    def verify(req: GenerateRequest):
        try:
            cfg = _cfg(req.config)
            samples = engine.build_sequence(req.digits, cfg)
        except (InvalidDigitError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"analysis": ToneVerifier().verify_sequence(samples, cfg.sample_rate)}

    @app.post("/api/play")
    async def play(req: PlayRequest):
        if not audio.available:
            raise HTTPException(503, "No audio output available on server")
        try:
            cfg = _cfg(req.config)
            samples = engine.build_sequence(req.digits, cfg)
        except (InvalidDigitError, ValueError) as e:
            raise HTTPException(400, str(e))

        def _do_play():
            if not play_lock.acquire(blocking=False):
                raise RuntimeError("Audio device busy")
            try:
                audio.play(samples, cfg.sample_rate, req.device)
            finally:
                play_lock.release()

        try:
            await asyncio.to_thread(_do_play)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"status": "played", "duration": len(samples) / cfg.sample_rate}

    @app.get("/api/devices")
    def devices():
        return {"backend": audio.backend, "devices": audio.get_devices()}

    @app.get("/api/presets")
    def list_presets():
        return {"presets": presets.list_all()}

    @app.post("/api/presets")
    def save_preset(p: PresetModel):
        try:
            presets.save(
                Preset(p.name, p.digits, Config.from_dict(p.config), p.description, p.tags)
            )
        except PresetError as e:
            raise HTTPException(400, str(e))
        return {"status": "saved"}

    @app.delete("/api/presets/{name}")
    def delete_preset(name: str):
        try:
            presets.delete(name)
        except PresetError as e:
            raise HTTPException(404, str(e))
        return {"status": "deleted"}

    # ---- macros ---------------------------------------------------------

    @app.get("/api/macros")
    def list_macros():
        return {"macros": macros.list_all()}

    @app.post("/api/macros")
    def save_macro(m: MacroModel):
        try:
            macros.save(Macro(m.name, m.steps, m.description, m.pinned))
        except MacroError as e:
            raise HTTPException(400, str(e))
        return {"status": "saved"}

    @app.delete("/api/macros/{name}")
    def delete_macro(name: str):
        try:
            macros.delete(name)
        except MacroError as e:
            raise HTTPException(404, str(e))
        return {"status": "deleted"}

    @app.post("/api/macros/{name}/play")
    async def play_macro(name: str):
        if not audio.available:
            raise HTTPException(503, "No audio output available on server")
        try:
            macro = macros.load(name)
            samples = engine.build_macro(macro.steps, settings.defaults, presets.load)
        except (MacroError, PresetError, InvalidDigitError, ValueError) as e:
            raise HTTPException(400, str(e))

        def _do_play():
            if not play_lock.acquire(blocking=False):
                raise RuntimeError("Audio device busy")
            try:
                audio.play(samples, settings.defaults.sample_rate, None)
            finally:
                play_lock.release()

        try:
            await asyncio.to_thread(_do_play)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"status": "played",
                "duration": len(samples) / settings.defaults.sample_rate}

    @app.post("/api/macros/{name}/render")
    def render_macro(name: str):
        try:
            macro = macros.load(name)
            samples = engine.build_macro(macro.steps, settings.defaults, presets.load)
        except (MacroError, PresetError, InvalidDigitError, ValueError) as e:
            raise HTTPException(400, str(e))
        sr = settings.defaults.sample_rate
        wav = engine.to_wav_bytes(samples, sr)
        return {
            "audio": base64.b64encode(wav).decode("ascii"),
            "duration": len(samples) / sr,
            "sample_rate": sr,
        }

    @app.websocket("/ws/audio")
    async def audio_stream(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_json()
                try:
                    cfg = _cfg(msg.get("config", {}))
                    chunks = list(engine.generate_chunks(msg.get("digits", ""), cfg))
                except (InvalidDigitError, ValueError) as e:
                    await ws.send_json({"error": str(e)})
                    continue
                await ws.send_json(
                    {"meta": {"sample_rate": cfg.sample_rate, "chunks": len(chunks)}}
                )
                for chunk in chunks:
                    await ws.send_bytes(chunk)
                await ws.send_json({"done": True})
        except WebSocketDisconnect:
            return

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:  # pragma: no cover

        @app.get("/")
        def _no_static():
            return JSONResponse({"error": "static assets missing"}, status_code=500)

    return app


def run_web(settings: Settings, host: str, port: int, open_browser: bool = False) -> None:
    import uvicorn

    app = create_app(settings)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port)
