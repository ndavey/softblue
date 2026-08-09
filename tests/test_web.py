import base64

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from softblue.config import Settings  # noqa: E402
from softblue.web import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFTBLUE_HOME", str(tmp_path / ".softblue"))
    return TestClient(create_app(Settings()))


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_generate_returns_wav(client):
    r = client.post("/api/generate", json={"digits": "123", "config": {"seize_duration": 0.1}})
    assert r.status_code == 200
    body = r.json()
    wav = base64.b64decode(body["audio"])
    assert wav[:4] == b"RIFF" and body["duration"] > 0


def test_generate_invalid_digit(client):
    r = client.post("/api/generate", json={"digits": "12@"})
    assert r.status_code == 400
    assert "not a valid MF digit" in r.json()["detail"]


def test_presets_crud(client):
    r = client.post("/api/presets", json={"name": "webp", "digits": "9", "config": {}})
    assert r.status_code == 200
    assert any(p["name"] == "webp" for p in client.get("/api/presets").json()["presets"])
    assert client.delete("/api/presets/webp").status_code == 200


def test_preset_path_traversal_rejected(client):
    r = client.post("/api/presets", json={"name": "../evil", "config": {}})
    assert r.status_code == 400


def test_verify_endpoint(client):
    r = client.post("/api/verify", json={"digits": "", "config": {"seize_only": True, "seize_duration": 0.3}})
    assert r.status_code == 200
    analysis = r.json()["analysis"]
    assert any(
        any(f["frequency"] == 2600 for f in chunk["frequencies"])
        for chunk in analysis if not chunk["silent"]
    )


def test_websocket_stream(client):
    with client.websocket_connect("/ws/audio") as ws:
        ws.send_json({"digits": "1", "config": {"seize_duration": 0.1}})
        meta = ws.receive_json()
        assert meta["meta"]["chunks"] > 0
        first = ws.receive_bytes()
        assert len(first) % 2 == 0


# ---- SIP endpoints -----------------------------------------------------------


@pytest.fixture
def sip_home(tmp_path, monkeypatch):
    """Point SOFTBLUE_HOME at a scratch dir so the real sip.yaml is untouched."""
    monkeypatch.setenv("SOFTBLUE_HOME", str(tmp_path))
    monkeypatch.delenv("SOFTBLUE_SIP_HOST", raising=False)
    monkeypatch.delenv("SOFTBLUE_SIP_PORT", raising=False)
    monkeypatch.delenv("SOFTBLUE_SIP_USER", raising=False)
    monkeypatch.delenv("SOFTBLUE_SIP_PASSWORD", raising=False)
    return tmp_path


def test_sip_status_reports_unconfigured(sip_home):
    c = TestClient(create_app(Settings()))
    d = c.get("/api/sip/status").json()
    assert d["configured"] is False
    assert "host" in d["detail"].lower()


def test_sip_status_never_returns_the_password(sip_home):
    (sip_home / "sip.yaml").write_text(
        "host: pbx.local\nuser: softphone\npassword: hunter2\n")
    c = TestClient(create_app(Settings()))
    d = c.get("/api/sip/status").json()
    assert d["configured"] is True
    assert d["account"]["has_password"] is True
    assert "hunter2" not in str(d)
    assert "password" not in d["account"]


def test_sip_status_surfaces_bad_yaml(sip_home):
    (sip_home / "sip.yaml").write_text("host: pbx\nuser: x\t\t # tab\n")
    c = TestClient(create_app(Settings()))
    d = c.get("/api/sip/status").json()
    assert d["configured"] is False
    assert "yaml" in d["detail"].lower()


def test_sip_call_dials_and_returns_analysis(sip_home):
    """Full path: HTTP -> SipCall -> RTP -> blind analysis of the far end."""
    import sys

    sys.path.insert(0, "tests")
    from test_sipcall import FakeUAS

    uas = FakeUAS().start()
    try:
        (sip_home / "sip.yaml").write_text(
            f"host: 127.0.0.1\nport: {uas.port}\nuser: softphone\n"
            f"password: secret\n")
        c = TestClient(create_app(Settings()))
        r = c.post("/api/sip/call", json={
            "extension": "1234", "digits": "q",
            "config": {"mode": "phreakme_coin", "inter_digit_gap": 0.1},
            "listen": 1.0, "wait_before": 0.2, "timeout": 5.0,
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["codec"] == "PCMU"
        assert d["audio"]          # echoed audio came back as a WAV
        tones = [s for s in d["segments"] if not s["silent"]]
        assert len(tones) == 2     # our quarter, echoed: 1700 then 2200
        assert abs(tones[0]["freqs"][0] - 1700) < 25
        assert abs(tones[1]["freqs"][0] - 2200) < 25
    finally:
        uas.stop()


def test_sip_call_reports_a_dead_pbx(sip_home):
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (sip_home / "sip.yaml").write_text(
        f"host: 127.0.0.1\nport: {port}\nuser: u\npassword: p\n")
    c = TestClient(create_app(Settings()))
    r = c.post("/api/sip/call",
               json={"extension": "1", "listen": 0.1, "timeout": 1.0})
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"]


def test_sip_call_rejects_bad_digits(sip_home):
    (sip_home / "sip.yaml").write_text("host: 127.0.0.1\nuser: u\npassword: p\n")
    c = TestClient(create_app(Settings()))
    r = c.post("/api/sip/call", json={
        "extension": "1", "digits": "ZZZ",
        "config": {"mode": "phreakme_coin"}, "listen": 0.1})
    assert r.status_code == 400


def test_sip_call_runs_a_dial_string(sip_home):
    """The real flow: press 2 for the section, pause, dial the number."""
    import sys

    sys.path.insert(0, "tests")
    from test_sipcall import DtmfUAS

    uas = DtmfUAS().start()
    try:
        (sip_home / "sip.yaml").write_text(
            f"host: 127.0.0.1\nport: {uas.port}\nuser: '4242'\npassword: secret\n")
        c = TestClient(create_app(Settings()))
        r = c.post("/api/sip/call", json={
            "extension": "200", "dial": "2;212-555-1337",
            "listen": 0.3, "wait_before": 0.1, "timeout": 5.0,
        })
        assert r.status_code == 200, r.text
        assert uas.digits() == "22125551337"
        kinds = [t["kind"] for t in r.json()["timeline"]]
        assert kinds == ["dtmf", "wait", "dtmf"]
    finally:
        uas.stop()


def test_sip_call_rejects_a_bad_dial_string(sip_home):
    (sip_home / "sip.yaml").write_text("host: 127.0.0.1\nuser: u\npassword: p\n")
    c = TestClient(create_app(Settings()))
    r = c.post("/api/sip/call",
               json={"extension": "1", "dial": "2[zz]", "listen": 0.1})
    assert r.status_code == 400
    assert "coin" in r.json()["detail"]


def test_redbox_schemes_endpoint_lists_the_search_space():
    from softblue.redbox import MF_ALPHABET, default_candidates

    c = TestClient(create_app(Settings()))
    d = c.get("/api/redbox/schemes").json()
    n = len(MF_ALPHABET)
    assert d["alphabet"] == list(MF_ALPHABET)
    assert len(d["schemes"]) == len(default_candidates())
    assert len(d["schemes"]) >= n * (n - 1)     # every ordered pair, plus variants
    assert d["schemes"][0]["is_control"] is True
    # Exactly one control, and no two schemes share a name — the index the UI
    # sends and the name the log records both have to mean one thing.
    assert sum(s["is_control"] for s in d["schemes"]) == 1
    labels = [s["label"] for s in d["schemes"]]
    assert len(set(labels)) == len(labels)


def test_redbox_schemes_endpoint_ships_playable_tables():
    """The page has to be able to synthesise a candidate with no further calls."""
    c = TestClient(create_app(Settings()))
    schemes = c.get("/api/redbox/schemes").json()["schemes"]
    control, second = schemes[0], schemes[1]
    assert control["coin_spec"]["q"] == [
        [[1700.0], 0.06, -3.0], [None, 0.06, 0.0],
        [[2200.0], 0.06, -3.0], [None, 0.06, 0.0]]
    assert second["coin_spec"]["q"][0][0] == [second["freq_a"]]
    assert second["coin_spec"]["q"][2][0] == [second["freq_b"]]
    assert set(control["coin_spec"]) == set("ndq$cr")


def test_generate_accepts_a_candidate_coin_table(client, tmp_path):
    """What the keypad sends once a candidate scheme is selected."""
    from softblue.engine import ToneEngine
    from softblue.sweep import scan_segments

    spec = {"q": [[[1500], 0.06, -3.0], [None, 0.06, 0.0],
                  [[2200], 0.06, -3.0], [None, 0.06, 0.0]]}
    r = client.post("/api/generate", json={
        "digits": "q",
        "config": {"mode": "phreakme_coin", "coin_spec": spec}})
    assert r.status_code == 200, r.text

    wav = tmp_path / "q.wav"
    wav.write_bytes(base64.b64decode(r.json()["audio"]))
    x, sr = ToneEngine().read_wav(str(wav))
    tones = [s for s in scan_segments(x, sr) if not s.silent]
    assert len(tones) == 2
    assert tones[0].freqs[0] == pytest.approx(1500, abs=30)
    assert tones[1].freqs[0] == pytest.approx(2200, abs=30)


def test_sip_call_applies_the_selected_redbox_scheme(sip_home):
    """Scheme 2 is the top prediction, so the quarter must lead with 1500."""
    import sys

    sys.path.insert(0, "tests")
    from test_sipcall import FakeUAS

    from softblue.engine import ToneEngine
    from softblue.sweep import scan_segments

    uas = FakeUAS().start()          # echoes RTP back to us
    try:
        (sip_home / "sip.yaml").write_text(
            f"host: 127.0.0.1\nport: {uas.port}\nuser: '4242'\npassword: p\n")
        c = TestClient(create_app(Settings()))
        r = c.post("/api/sip/call", json={
            "extension": "200", "digits": "q", "redbox_scheme": 2,
            "config": {"mode": "phreakme_coin", "inter_digit_gap": 0.1},
            "listen": 0.6, "wait_before": 0.1, "timeout": 5.0})
        assert r.status_code == 200, r.text
        d = r.json()
        assert "1500->2200" in d["scheme"]
        import base64
        wav = sip_home / "echo.wav"
        wav.write_bytes(base64.b64decode(d["audio"]))
        x, sr = ToneEngine().read_wav(str(wav))
        tones = [s for s in scan_segments(x, sr) if not s.silent]
        assert len(tones) == 2
        assert tones[0].freqs[0] == pytest.approx(1500, abs=30)
        assert tones[1].freqs[0] == pytest.approx(2200, abs=30)
    finally:
        uas.stop()


def test_bad_redbox_scheme_index_is_rejected(sip_home):
    from softblue.redbox import default_candidates

    (sip_home / "sip.yaml").write_text("host: 127.0.0.1\nuser: u\npassword: p\n")
    c = TestClient(create_app(Settings()))
    r = c.post("/api/sip/call",
               json={"extension": "1", "redbox_scheme": 999, "listen": 0.1})
    assert r.status_code == 400
    assert f"1-{len(default_candidates())}" in r.json()["detail"]
