"""Self-hosted web interface (FastAPI + WebSocket streaming).

Security note: this server exposes audio playback control with no auth. It is
intended only for the isolated PhreakMe / ProjectMF lab. Binding to 0.0.0.0
makes it reachable on the local network — do so deliberately.

That warning is sharper now that ``/api/sip/call`` exists: anyone who can reach
this server can place calls through your PBX account, because the credentials
live server-side. The password itself is never sent to or from the browser, but
the *ability to dial* is only as protected as the listening socket. Keep the
default 127.0.0.1 bind unless you have a reason not to.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
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


class SipCallRequest(BaseModel):
    """A dial request. Note there is deliberately no password field — the
    server reads credentials from sip.yaml / the environment, so the secret
    never travels to or from the browser."""

    extension: str
    dial: str = ""
    digits: str = ""
    # Index (1-based) into /api/redbox/schemes; overrides the built-in coin table.
    redbox_scheme: int | None = None
    config: dict = Field(default_factory=dict)
    listen: float = 5.0
    wait_before: float = 1.0
    timeout: float = 30.0
    no_register: bool = False
    # No host/port/user override on purpose. Letting the request name the PBX
    # would make this endpoint a credential oracle: softblue would REGISTER to
    # the attacker's host, receive a 401 with a chosen realm+nonce, and hand back
    # a digest of the stored sip.yaml password. Change the target in sip.yaml, or
    # use the CLI's --host, which crosses no privilege boundary.


def create_app(settings: Settings) -> FastAPI:
    # Declared before the app so the lifespan handler can close it on shutdown.
    sip_state: dict = {"session": None, "key": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        sess = sip_state.get("session")
        if sess is not None:
            try:
                sess.close()          # sends Expires: 0, leaves no stale contact
            except Exception:
                pass
            sip_state.update(session=None, key=None)

    app = FastAPI(title="SoftBlue Web", version="1.0.0", lifespan=lifespan)
    engine = ToneEngine()
    audio = AudioOutput()
    presets = PresetManager(settings.preset_dir)
    macros = MacroManager()
    play_lock = threading.Lock()
    sip_lock = threading.Lock()
    # One long-lived SIP registration for the server, not one per dial. Keeps the
    # PBX contact stable (and answers its qualify OPTIONS) instead of re-binding
    # a fresh ephemeral port on every request. See `sip_state` above.

    def _sip_session(account):
        from .sipcall import SipSession

        key = (account.host, account.port, account.user, account.register)
        sess = sip_state["session"]
        if sess is not None and sip_state["key"] == key:
            return sess
        if sess is not None:                 # account changed — replace it
            try:
                sess.close()
            except Exception:
                pass
        sess = SipSession(account, timeout=30.0)
        if account.register:
            sess.register()
        sip_state.update(session=sess, key=key)
        return sess


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

    # ---- SIP ------------------------------------------------------------
    #
    # The browser cannot open a UDP socket, so the call runs here and the UI
    # only drives it. Credentials stay server-side: the request model has no
    # password field and /api/sip/status reports presence, never the value.

    @app.get("/api/redbox/schemes")
    def redbox_schemes(limit: int = 64):
        """Candidate coin schemes, most-likely first.

        PhreakMe's coin table is generated from one frequency pair, so a change
        of frequencies leaves the structure intact and only moves the pair. The
        ranked analysis leads; the remaining ordered pairs follow it.

        ``coin_spec`` is included per scheme so the page can play a candidate
        through Web Audio without a round trip — the browser is the thing held
        against the handset, and it has to keep working when the venue's wifi
        does not.
        """
        from .redbox import MF_ALPHABET, default_candidates

        return {"alphabet": list(MF_ALPHABET),
                "schemes": [{"index": i, "describe": s.describe(),
                             "is_control": s.is_control,
                             "coin_spec": s.coin_spec(), **s.to_dict()}
                            for i, s in enumerate(default_candidates()[:limit], 1)]}

    @app.get("/api/sip/status")
    def sip_status():
        from .sipcall import SipError, load_account

        try:
            return {"configured": True, "account": load_account().public()}
        except SipError as e:
            return {"configured": False, "detail": str(e)}

    @app.post("/api/sip/call")
    async def sip_call(req: SipCallRequest):
        from .dialstring import DialStringError
        from .dialstring import parse as parse_dial
        from .sipcall import RTP_SAMPLE_RATE, SipCall, SipError, load_account
        from .sweep import scan_segments

        try:
            steps = parse_dial(req.dial) if req.dial else []
        except DialStringError as e:
            raise HTTPException(400, f"dial string: {e}")

        try:
            account = load_account(no_register=req.no_register)
        except SipError as e:
            raise HTTPException(400, str(e))

        scheme = None
        if req.redbox_scheme is not None:
            from .redbox import default_candidates
            all_schemes = default_candidates()
            if not 1 <= req.redbox_scheme <= len(all_schemes):
                raise HTTPException(
                    400, f"redbox_scheme must be 1-{len(all_schemes)}")
            scheme = all_schemes[req.redbox_scheme - 1]

        samples = None
        if req.digits:
            try:
                extra = {"coin_spec": scheme.coin_spec()} if scheme else {}
                cfg = _cfg({**req.config, **extra,
                            "sample_rate": RTP_SAMPLE_RATE})
                samples = engine.build_sequence(req.digits, cfg)
            except (InvalidDigitError, ValueError) as e:
                raise HTTPException(400, str(e))

        class _Busy(Exception):
            """Distinct from RuntimeError on purpose — SipError subclasses
            RuntimeError, so catching that for the busy case would report every
            dial failure as 'a call is already in progress'."""

        def _do_call():
            # One call at a time: a second dial would fight the first for the
            # account registration and interleave RTP.
            if not sip_lock.acquire(blocking=False):
                raise _Busy("A SIP call is already in progress")
            timeline = []
            try:
                call = SipCall(account, timeout=req.timeout,
                               session=_sip_session(account))
                with call:
                    call.dial(req.extension)
                    codec = call.rtp.payload_type
                    remote = call.rtp.remote
                    if req.wait_before > 0:
                        call.listen(req.wait_before)
                    if steps:
                        coin_cfg = _cfg({
                            "mode": "phreakme_coin",
                            "sample_rate": RTP_SAMPLE_RATE,
                            **({"coin_spec": scheme.coin_spec()} if scheme else {})})
                        timeline.extend(call.run_steps(
                            steps,
                            lambda sym: engine.build_sequence(sym, coin_cfg)))
                    if samples is not None:
                        call.play(samples)
                    heard = call.listen(req.listen)
                return heard, codec, remote, timeline
            finally:
                sip_lock.release()

        try:
            heard, codec, remote, timeline = await asyncio.to_thread(_do_call)
        except _Busy as e:
            raise HTTPException(409, str(e))
        except SipError as e:
            raise HTTPException(502, str(e))
        except OSError as e:
            raise HTTPException(502, f"network error placing the call: {e}")

        from .g711 import CODECS

        segs = [
            {"start": round(s.start_s, 3), "dur_ms": round(s.dur_ms, 1),
             "freqs": [round(f) for f in s.freqs],
             "level_dbfs": (None if s.silent else round(s.level_dbfs, 1)),
             "silent": s.silent}
            for s in scan_segments(heard, RTP_SAMPLE_RATE)
        ] if len(heard) else []

        return {
            "status": "ok",
            "codec": CODECS[codec][0],
            "scheme": scheme.describe() if scheme else None,
            "remote": f"{remote[0]}:{remote[1]}" if remote else None,
            "duration": len(heard) / RTP_SAMPLE_RATE,
            "timeline": timeline,
            "segments": segs,
            "audio": (base64.b64encode(
                engine.to_wav_bytes(heard, RTP_SAMPLE_RATE)).decode("ascii")
                if len(heard) else None),
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
