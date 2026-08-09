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


# ---- redbox sweep delivery modes ---------------------------------------------


def test_redbox_sweep_infers_audio_without_extension():
    """No EXTENSION means there is nothing to dial, so deliver acoustically."""
    r = run(["redbox", "sweep", "--dry-run", "-n", "3"])
    assert r.exit_code == 0, r.output
    assert "played locally" in r.output
    assert "1700->2200Hz" in r.output


def test_redbox_sweep_infers_sip_with_extension():
    r = run(["redbox", "sweep", "2195002600", "--dry-run", "-n", "2"])
    assert r.exit_code == 0, r.output
    assert "dial 2195002600" in r.output
    assert "no-coin control" in r.output


def test_redbox_sweep_sip_without_extension_is_rejected():
    r = run(["redbox", "sweep", "--via", "sip", "--dry-run"])
    assert r.exit_code != 0
    assert "needs an EXTENSION" in r.output


def test_redbox_sweep_audio_ignores_extension_when_forced():
    """--via audio is explicit; it must not start dialling."""
    r = run(["redbox", "sweep", "2195002600", "--via", "audio",
             "--dry-run", "-n", "2"])
    assert r.exit_code == 0, r.output
    assert "played locally" in r.output
    assert "dial 2195002600" not in r.output


def test_redbox_schemes_and_export_need_no_sip(tmp_path):
    """The offline paths must work with no PBX configured at all."""
    r = run(["redbox", "schemes", "-n", "2"])
    assert r.exit_code == 0 and "control" in r.output
    out = tmp_path / "wavs"
    r = run(["redbox", "export", str(out), "-n", "2", "--symbols", "nq"])
    assert r.exit_code == 0, r.output
    wavs = sorted(p.name for p in out.glob("*.wav"))
    assert len(wavs) == 4, wavs
    assert (out / "schemes.json").exists()


def test_redbox_schemes_leads_with_the_shipped_ranking():
    """The default order is the analysis, not the bare enumeration."""
    r = run(["redbox", "schemes", "-n", "2"])
    assert r.exit_code == 0, r.output
    assert "1500->2200Hz" in r.output          # ranked #2
    r = run(["redbox", "schemes", "--all-pairs", "-n", "2"])
    assert r.exit_code == 0, r.output
    assert "2200->1700Hz" in r.output          # heuristic #2
    assert "1500->2200Hz" not in r.output


def test_redbox_spec_renders_an_ad_hoc_pair(tmp_path):
    """`analyze` says the pair moved; this plays it without a code edit."""
    import json

    out = tmp_path / "hit.json"
    r = run(["redbox", "spec", "-f", "1500,2200", "-o", str(out)])
    assert r.exit_code == 0, r.output
    spec = json.loads(out.read_text())
    assert spec["q"][0][0] == [1500.0] and spec["q"][2][0] == [2200.0]
    assert spec["n"][0][2] == -6.0 and spec["d"][0][2] == -3.0
    # and it round-trips straight back into a playable sequence
    wav = tmp_path / "q.wav"
    r = run(["generate", "q", "-o", str(wav), "-m", "phreakme_coin",
             "--coin-spec", str(out)])
    assert r.exit_code == 0, r.output
    assert wav.exists()


def test_redbox_spec_rejects_a_malformed_pair():
    r = run(["redbox", "spec", "-f", "1500"])
    assert r.exit_code != 0
    assert "expected 'A,B'" in r.output


def test_redbox_spec_by_index_matches_the_listing():
    r = run(["redbox", "spec", "-s", "2"])
    assert r.exit_code == 0, r.output
    assert "1500" in r.output and "2200" in r.output
