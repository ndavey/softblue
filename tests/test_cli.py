from click.testing import CliRunner

from softblue.cli import cli
from softblue.engine import ToneEngine


def run(args, **kw):
    return CliRunner().invoke(cli, args, **kw)


def test_generate_writes_wav(tmp_path):
    out = tmp_path / "c.wav"
    r = run(["generate", "1234", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    data, sr = ToneEngine().read_wav(str(out))
    assert sr == 8000 and len(data) > 0


def test_generate_invalid_digit():
    r = run(["generate", "12@", "-o", "/dev/null"])
    assert r.exit_code != 0
    assert "not a valid MF digit" in r.output


def test_generate_short_alias_flags(tmp_path):
    out = tmp_path / "s.wav"
    r = run(["generate", "1", "-o", str(out), "--seize", "0.1", "--wink", "0.0"])
    assert r.exit_code == 0, r.output
    # ~0.1s seize + tiny KP/digit/ST/gaps; well under 1s
    data, sr = ToneEngine().read_wav(str(out))
    assert len(data) / sr < 1.0


def test_seize_only_flag(tmp_path):
    out = tmp_path / "z.wav"
    r = run(["generate", "", "-o", str(out), "--seize", "0.5", "--seize-only"])
    assert r.exit_code == 0, r.output
    data, sr = ToneEngine().read_wav(str(out))
    assert abs(len(data) / sr - 0.5) < 0.02


def test_devices_runs():
    r = run(["devices"])
    assert r.exit_code == 0
    assert "Backend:" in r.output


def test_preset_save_list_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFTBLUE_HOME", str(tmp_path / ".softblue"))
    r = run(["preset", "save", "t1", "--digits", "55", "--seize", "1.5"])
    assert r.exit_code == 0, r.output
    r = run(["preset", "list"])
    assert "t1" in r.output
    r = run(["preset", "delete", "t1"])
    assert r.exit_code == 0


def test_preset_save_rejects_bad_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFTBLUE_HOME", str(tmp_path / ".softblue"))
    r = run(["preset", "save", "../evil", "--digits", "1"])
    assert r.exit_code != 0
    assert "invalid preset name" in r.output


def test_verify_detects_seize(tmp_path):
    wav = tmp_path / "v.wav"
    run(["generate", "", "-o", str(wav), "--seize", "0.5", "--seize-only"])
    r = run(["verify", str(wav)])
    assert r.exit_code == 0
    assert "2600Hz" in r.output
