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
