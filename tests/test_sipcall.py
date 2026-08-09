"""SIP/RTP transport tests, including a loopback UAS for the full call flow."""

from __future__ import annotations

import socket
import threading
import time

import numpy as np
import pytest

from softblue import g711
from softblue.sipcall import (
    RTP_SAMPLE_RATE,
    SipAccount,
    SipCall,
    SipError,
    build_auth_header,
    build_sdp,
    digest_response,
    parse_auth_header,
    parse_response,
    parse_sdp,
)

# ---- G.711 -------------------------------------------------------------------


@pytest.mark.parametrize("pt", [g711.PCMU, g711.PCMA])
def test_codec_is_idempotent_on_its_own_lattice(pt):
    """decode->encode must be a fixed point, or RTP audio degrades every hop."""
    data = bytes(range(256))
    back = g711.encode(g711.decode(data, pt), pt)
    if pt == g711.PCMU:
        # 0x7F is mu-law negative zero; encoders canonically emit 0xFF for zero.
        assert sum(a != b for a, b in zip(data, back)) == 1
        assert back[0x7F] == 0xFF
    else:
        assert back == data


@pytest.mark.parametrize("pt", [g711.PCMU, g711.PCMA])
def test_codec_round_trip_snr(pt):
    t = np.arange(480) / RTP_SAMPLE_RATE
    pcm = g711.float_to_pcm16(10 ** (-3 / 20) * np.sin(2 * np.pi * 1700 * t))
    rt = g711.decode(g711.encode(pcm, pt), pt).astype(np.float64)
    noise = float(np.sum((rt - pcm) ** 2)) or 1e-12
    snr = 10 * np.log10(float(np.sum(pcm.astype(np.float64) ** 2)) / noise)
    assert snr > 35.0          # G.711 is nominally ~38 dB


def test_coin_levels_survive_the_codec():
    """The 3 dB nickel/dime split must not be blurred by companding.

    Measured as RMS, not peak: mu-law's step size grows with amplitude, so a
    peak reading carries ~0.3 dB of quantisation jitter. Energy over the burst
    is both steadier and what a real detector computes (PhreakMe's own coin
    detector is a Goertzel power measurement).
    """
    t = np.arange(480) / RTP_SAMPLE_RATE
    out = []
    for dbfs in (-6.0, -3.0):
        pcm = g711.float_to_pcm16(10 ** (dbfs / 20) * np.sin(2 * np.pi * 1700 * t))
        rt = g711.decode(g711.encode(pcm, g711.PCMU), g711.PCMU).astype(np.float64)
        out.append(20 * np.log10(np.sqrt(np.mean(rt**2)) / 32767))
    assert out[1] - out[0] == pytest.approx(3.0, abs=0.1)


# ---- digest auth -------------------------------------------------------------


def test_digest_matches_rfc2617_vector():
    assert digest_response(
        "Mufasa", "Circle Of Life", "testrealm@host.com",
        "dcd98b7102dd2f0e8b11d0f600bfb0c093", "GET", "/dir/index.html",
        qop="auth", nc="00000001", cnonce="0a4f113b",
    ) == "6629fae49393a05397450978507c4ef1"


def test_parse_auth_header_handles_quoted_and_bare():
    p = parse_auth_header('Digest realm="asterisk", nonce="abc123", '
                          'algorithm=MD5, qop="auth"')
    assert p["realm"] == "asterisk"
    assert p["nonce"] == "abc123"
    assert p["algorithm"] == "MD5"


def test_auth_header_omits_qop_when_not_offered():
    acct = SipAccount(host="pbx", user="u", password="p")
    hdr = build_auth_header(acct, {"realm": "r", "nonce": "n"}, "INVITE", "sip:x")
    assert "qop" not in hdr and "cnonce" not in hdr
    assert 'username="u"' in hdr


def test_password_is_not_in_repr():
    assert "hunter2" not in repr(SipAccount(host="pbx", password="hunter2"))


# ---- SDP / message parsing ---------------------------------------------------


def test_sdp_offers_only_encodable_codecs():
    sdp = build_sdp("10.0.0.1", 40000, (g711.PCMU, g711.PCMA))
    assert "m=audio 40000 RTP/AVP 0 8 101" in sdp
    assert "a=rtpmap:0 PCMU/8000" in sdp
    assert "a=ptime:20" in sdp


def test_parse_sdp_extracts_media_target():
    ip, port, pt = parse_sdp(
        "v=0\r\nc=IN IP4 192.168.1.5\r\nm=audio 12345 RTP/AVP 8 101\r\n")
    assert (ip, port, pt) == ("192.168.1.5", 12345, g711.PCMA)


def test_parse_sdp_reports_unusable_codec():
    _, _, pt = parse_sdp("c=IN IP4 1.2.3.4\r\nm=audio 999 RTP/AVP 111\r\n")
    assert pt is None


def test_parse_response_reads_status_and_headers():
    r = parse_response(b"SIP/2.0 401 Unauthorized\r\n"
                       b"WWW-Authenticate: Digest realm=\"x\"\r\n"
                       b"Content-Length: 0\r\n\r\n")
    assert r.code == 401 and r.reason == "Unauthorized"
    assert "Digest" in r.header("www-authenticate")


# ---- loopback UAS ------------------------------------------------------------


class FakeUAS:
    """A minimal Asterisk stand-in: challenges once, answers, echoes RTP."""

    def __init__(self, require_auth: bool = True, codec: int = g711.PCMU):
        self.require_auth = require_auth
        self.codec = codec
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 0))
        self.sip.settimeout(0.2)
        self.port = self.sip.getsockname()[1]
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0))
        self.rtp.settimeout(0.2)
        self.rtp_port = self.rtp.getsockname()[1]
        self.authed_methods: list[str] = []
        self.rtp_in = 0
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._sip_loop, daemon=True),
                         threading.Thread(target=self._rtp_loop, daemon=True)]

    def start(self):
        for t in self._threads:
            t.start()
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self.sip.close()
        self.rtp.close()

    def _reply(self, req: str, addr, code: int, reason: str, extra="", body=""):
        get = lambda n: next(  # noqa: E731
            (l.split(":", 1)[1].strip() for l in req.split("\r\n")
             if l.lower().startswith(n)), "")
        to = get("to:")
        if code >= 200 and "tag=" not in to:
            to += ";tag=uastag"
        msg = (f"SIP/2.0 {code} {reason}\r\n"
               f"Via: {get('via:')}\r\nFrom: {get('from:')}\r\nTo: {to}\r\n"
               f"Call-ID: {get('call-id:')}\r\nCSeq: {get('cseq:')}\r\n"
               f"Contact: <sip:uas@127.0.0.1:{self.port}>\r\n"
               f"{extra}Content-Length: {len(body)}\r\n\r\n{body}")
        self.sip.sendto(msg.encode(), addr)

    def _sip_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sip.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            req = data.decode(errors="replace")
            method = req.split(None, 1)[0]
            has_auth = "authorization:" in req.lower()
            if method == "ACK":
                continue
            if self.require_auth and not has_auth and method in ("REGISTER", "INVITE"):
                self._reply(req, addr, 401, "Unauthorized",
                            'WWW-Authenticate: Digest realm="asterisk", '
                            'nonce="deadbeef", qop="auth"\r\n')
                continue
            if has_auth:
                self.authed_methods.append(method)
            if method == "INVITE":
                sdp = (f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
                       f"c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                       f"m=audio {self.rtp_port} RTP/AVP {self.codec}\r\n")
                self._reply(req, addr, 200, "OK", body=sdp)
            else:
                self._reply(req, addr, 200, "OK")

    def _rtp_loop(self):
        while not self._stop.is_set():
            try:
                pkt, addr = self.rtp.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            self.rtp_in += 1
            self.rtp.sendto(pkt, addr)      # echo back


@pytest.fixture
def uas():
    s = FakeUAS().start()
    yield s
    s.stop()


def _account(uas, **kw):
    return SipAccount(host="127.0.0.1", port=uas.port, user="softphone",
                      password="secret", **kw)


def test_full_call_flow_registers_and_connects(uas):
    call = SipCall(_account(uas), timeout=5.0)
    with call:
        call.dial("1234")
        assert call.connected
        assert call.rtp.payload_type == g711.PCMU
    assert "REGISTER" in uas.authed_methods
    assert "INVITE" in uas.authed_methods


def test_audio_reaches_the_far_end_intact(uas):
    """The whole point: tones must survive generation -> RTP -> the wire."""
    call = SipCall(_account(uas), timeout=5.0)
    with call:
        call.dial("1234")
        t = np.arange(RTP_SAMPLE_RATE // 2) / RTP_SAMPLE_RATE
        call.play(10 ** (-3 / 20) * np.sin(2 * np.pi * 1700 * t))
        time.sleep(0.4)
        heard = call.recorded()
    assert uas.rtp_in > 20
    assert len(heard) > RTP_SAMPLE_RATE // 4
    spec = np.abs(np.fft.rfft(heard * np.hanning(len(heard))))
    peak = float(np.fft.rfftfreq(len(heard), 1 / RTP_SAMPLE_RATE)[np.argmax(spec)])
    assert peak == pytest.approx(1700, abs=25)


def test_alaw_is_negotiated_when_the_pbx_picks_it():
    s = FakeUAS(codec=g711.PCMA).start()
    try:
        call = SipCall(_account(s), timeout=5.0)
        with call:
            call.dial("1234")
            assert call.rtp.payload_type == g711.PCMA
    finally:
        s.stop()


def test_unauthenticated_pbx_needs_no_password():
    s = FakeUAS(require_auth=False).start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="anon",
                          password="", register=False)
        call = SipCall(acct, timeout=5.0)
        with call:
            call.dial("1234")
            assert call.connected
    finally:
        s.stop()


def test_missing_password_reports_clearly(uas):
    acct = SipAccount(host="127.0.0.1", port=uas.port, user="softphone",
                      password="")
    call = SipCall(acct, timeout=5.0)
    try:
        with pytest.raises(SipError, match="SOFTBLUE_SIP_PASSWORD"):
            call.register()
    finally:
        call.hangup()


def test_wrong_sample_rate_is_refused(uas):
    call = SipCall(_account(uas), timeout=5.0)
    with call:
        call.dial("1234")
        with pytest.raises(SipError, match="8000"):
            call.play(np.zeros(100), sample_rate=44100)


def test_timeout_when_nothing_answers():
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    call = SipCall(SipAccount(host="127.0.0.1", port=port, password="x"),
                   timeout=1.0)
    try:
        with pytest.raises(SipError, match="timed out"):
            call.register()
    finally:
        call.hangup()


# ---- config loading ----------------------------------------------------------


@pytest.fixture
def sip_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFTBLUE_HOME", str(tmp_path))
    for v in ("SOFTBLUE_SIP_HOST", "SOFTBLUE_SIP_PORT", "SOFTBLUE_SIP_USER",
              "SOFTBLUE_SIP_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def test_unquoted_numeric_user_becomes_a_string(sip_home):
    """YAML reads `user: 4242` as an int; it must not reach the SIP headers."""
    from softblue.sipcall import load_account

    (sip_home / "sip.yaml").write_text("host: pbx\nuser: 4242\npassword: 1234\n")
    a = load_account()
    assert a.user == "4242" and isinstance(a.user, str)
    assert a.password == "1234" and isinstance(a.password, str)


def test_env_password_beats_the_file(sip_home, monkeypatch):
    from softblue.sipcall import load_account

    (sip_home / "sip.yaml").write_text("host: pbx\npassword: fromfile\n")
    monkeypatch.setenv("SOFTBLUE_SIP_PASSWORD", "fromenv")
    assert load_account().password == "fromenv"


def test_missing_host_is_reported(sip_home):
    from softblue.sipcall import SipError, load_account

    (sip_home / "sip.yaml").write_text("user: x\n")
    with pytest.raises(SipError, match="host"):
        load_account()


def test_no_register_flag_overrides_the_file(sip_home):
    from softblue.sipcall import load_account

    (sip_home / "sip.yaml").write_text("host: pbx\nregister: true\n")
    assert load_account().register is True
    assert load_account(no_register=True).register is False


def test_public_dict_excludes_the_password(sip_home):
    from softblue.sipcall import load_account

    (sip_home / "sip.yaml").write_text("host: pbx\npassword: hunter2\n")
    pub = load_account().public()
    assert pub["has_password"] is True
    assert "hunter2" not in str(pub) and "password" not in pub


# ---- RFC 4733 DTMF -----------------------------------------------------------


class DtmfUAS(FakeUAS):
    """Stand-in that decodes RFC 4733 events instead of echoing."""

    def __init__(self, telephone_event_pt=101, **kw):
        super().__init__(**kw)
        self.tel_pt = telephone_event_pt
        self.events = []          # (event, end_bit, duration, timestamp, marker)

    def _sip_loop(self):
        # Same as the parent but advertises telephone-event in the answer.
        while not self._stop.is_set():
            try:
                data, addr = self.sip.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            req = data.decode(errors="replace")
            method = req.split(None, 1)[0]
            has_auth = "authorization:" in req.lower()
            if method == "ACK":
                continue
            if self.require_auth and not has_auth and method in ("REGISTER", "INVITE"):
                self._reply(req, addr, 401, "Unauthorized",
                            'WWW-Authenticate: Digest realm="asterisk", '
                            'nonce="deadbeef", qop="auth"\r\n')
                continue
            if method == "INVITE":
                tel = (f"m=audio {self.rtp_port} RTP/AVP {self.codec} {self.tel_pt}\r\n"
                       f"a=rtpmap:{self.codec} PCMU/8000\r\n"
                       f"a=rtpmap:{self.tel_pt} telephone-event/8000\r\n"
                       if self.tel_pt else
                       f"m=audio {self.rtp_port} RTP/AVP {self.codec}\r\n")
                self._reply(req, addr, 200, "OK",
                            body=("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
                                  "c=IN IP4 127.0.0.1\r\nt=0 0\r\n" + tel))
            else:
                self._reply(req, addr, 200, "OK")

    def _rtp_loop(self):
        while not self._stop.is_set():
            try:
                pkt, _ = self.rtp.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            self.rtp_in += 1
            if len(pkt) < 16:
                continue
            pt = pkt[1] & 0x7F
            marker = bool(pkt[1] & 0x80)
            ts = int.from_bytes(pkt[4:8], "big")
            if self.tel_pt is not None and pt == self.tel_pt:
                ev, b, dur = pkt[12], pkt[13], int.from_bytes(pkt[14:16], "big")
                self.events.append((ev, bool(b & 0x80), dur, ts, marker))

    def digits(self):
        """Collapse the event stream back into the digits that were sent."""
        out, last_ts = [], None
        for ev, end, _dur, ts, _m in self.events:
            if not end and ts != last_ts:
                out.append(next(k for k, v in
                                __import__("softblue.sipcall", fromlist=["x"])
                                .DTMF_EVENTS.items() if v == ev))
                last_ts = ts
        return "".join(out)


def test_dtmf_is_sent_as_rfc4733_events():
    """dtmf_mode=rfc4733 ignores in-band audio, so these must be RTP events."""
    s = DtmfUAS().start()
    try:
        call = SipCall(_account(s), timeout=5.0)
        with call:
            call.dial("1234")
            assert call.rtp.telephone_event_pt == 101
            call.dtmf("2")
            time.sleep(0.2)
        assert s.digits() == "2"
        ends = [e for e in s.events if e[1]]
        assert len(ends) == 3          # RFC 4733: end retransmitted three times
        starts = [e for e in s.events if not e[1]]
        assert starts[0][4] is True    # marker on the first packet
        assert len({e[3] for e in starts}) == 1   # timestamp pinned for the event
        assert starts[-1][2] > starts[0][2]       # duration counts up
    finally:
        s.stop()


def test_dtmf_sends_a_full_number():
    s = DtmfUAS().start()
    try:
        call = SipCall(_account(s), timeout=5.0)
        with call:
            call.dial("1234")
            call.dtmf("2125551337", duration=0.04, gap=0.0)
            time.sleep(0.2)
        assert s.digits() == "2125551337"
    finally:
        s.stop()


def test_dtmf_refused_when_not_negotiated():
    """A PBX that offers no telephone-event must fail loudly, not silently."""
    s = DtmfUAS(telephone_event_pt=None).start()
    try:
        call = SipCall(_account(s), timeout=5.0)
        with call:
            call.dial("1234")
            assert call.rtp.telephone_event_pt is None
            with pytest.raises(SipError, match="telephone-event"):
                call.dtmf("2")
    finally:
        s.stop()


def test_run_steps_executes_a_dial_string():
    from softblue.dialstring import parse

    s = DtmfUAS().start()
    try:
        call = SipCall(_account(s), timeout=5.0)
        with call:
            call.dial("1234")
            tl = call.run_steps(parse("2;212-555-1337"),
                                render_coins=lambda c: np.zeros(80))
            time.sleep(0.2)
        assert s.digits() == "22125551337"
        assert [t["kind"] for t in tl] == ["dtmf", "wait", "dtmf"]
        assert tl[1]["at"] < tl[2]["at"]
    finally:
        s.stop()


# ---- transaction handling ----------------------------------------------------


class FailUAS(FakeUAS):
    """Rejects the INVITE, records whether the failure was ACKed."""

    def __init__(self, code=486, reason="Busy Here", **kw):
        super().__init__(**kw)
        self.code, self.reason = code, reason
        self.acks = 0
        self.requests = []

    def _sip_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sip.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            req = data.decode(errors="replace")
            method = req.split(None, 1)[0]
            cseq = next((l.split(":", 1)[1].strip() for l in req.split("\r\n")
                         if l.lower().startswith("cseq:")), "")
            self.requests.append((method, cseq))
            if method == "ACK":
                self.acks += 1
                continue
            if method == "INVITE":
                self._reply(req, addr, self.code, self.reason)
            else:
                self._reply(req, addr, 200, "OK")


def test_non_2xx_invite_is_acked():
    """RFC 3261 17.1.1.3: without this the PBX retransmits the failure for 32s."""
    s = FailUAS().start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="u",
                          password="", register=False)
        call = SipCall(acct, timeout=5.0)
        with pytest.raises(SipError, match="486"):
            call.dial("1234")
        call.hangup()
        time.sleep(0.2)
        assert s.acks == 1, f"expected one ACK, saw {s.requests}"
        invite_cseq = next(c for m, c in s.requests if m == "INVITE")
        ack_cseq = next(c for m, c in s.requests if m == "ACK")
        # Same transaction => same CSeq number, method differs.
        assert invite_cseq.split()[0] == ack_cseq.split()[0]
    finally:
        s.stop()


class RetransmitUAS(FakeUAS):
    """Challenges, then retransmits the 401 like timer G, and answers late."""

    def __init__(self, ring_delay=1.0, **kw):
        super().__init__(**kw)
        self.ring_delay = ring_delay
        self.challenged_at = None
        self.acks = 0

    def _sip_loop(self):
        import threading as _t
        while not self._stop.is_set():
            try:
                data, addr = self.sip.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            req = data.decode(errors="replace")
            method = req.split(None, 1)[0]
            if method == "ACK":
                self.acks += 1
                continue
            has_auth = "authorization:" in req.lower()
            if method == "INVITE" and not has_auth:
                chal = ('WWW-Authenticate: Digest realm="asterisk", '
                        'nonce="deadbeef", qop="auth"\r\n')
                self._reply(req, addr, 401, "Unauthorized", chal)
                # Timer G: resend the challenge while the second INVITE is in
                # flight. A client that matches on branch must ignore it.
                def _again(r=req, a=addr, c=chal):
                    time.sleep(0.5)
                    if not self._stop.is_set():
                        self._reply(r, a, 401, "Unauthorized", c)
                _t.Thread(target=_again, daemon=True).start()
                continue
            if method == "INVITE":
                def _answer(r=req, a=addr):
                    time.sleep(self.ring_delay)
                    if self._stop.is_set():
                        return
                    sdp = (f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
                           f"c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                           f"m=audio {self.rtp_port} RTP/AVP 0\r\n")
                    self._reply(r, a, 200, "OK", body=sdp)
                _t.Thread(target=_answer, daemon=True).start()
                continue
            self._reply(req, addr, 200, "OK")


def test_retransmitted_challenge_does_not_fail_the_call():
    """A stale 401 must not be mistaken for the answer to the retried INVITE."""
    s = RetransmitUAS(ring_delay=1.0).start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="u",
                          password="pw", register=False)
        call = SipCall(acct, timeout=6.0)
        with call:
            call.dial("1234")          # would raise "INVITE failed: 401" before
            assert call.connected
    finally:
        s.stop()


class HangupUAS(FakeUAS):
    """Answers, then hangs up mid-listen so we can prove we notice."""

    def __init__(self, after=0.6, **kw):
        super().__init__(**kw)
        self.after = after
        self.bye_acked = False
        self._peer = None

    def _sip_loop(self):
        import threading as _t
        while not self._stop.is_set():
            try:
                data, addr = self.sip.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            req = data.decode(errors="replace")
            method = req.split(None, 1)[0]
            if method == "ACK":
                continue
            if req.startswith("SIP/2.0"):
                if " 200 " in req.split("\r\n")[0]:
                    self.bye_acked = True
                continue
            if method == "INVITE":
                self._peer = addr
                self._invite = req
                sdp = (f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
                       f"c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                       f"m=audio {self.rtp_port} RTP/AVP {self.codec}\r\n")
                self._reply(req, addr, 200, "OK", body=sdp)

                def _bye(a=addr, r=req):
                    time.sleep(self.after)
                    if self._stop.is_set():
                        return
                    cid = next((l.split(":", 1)[1].strip() for l in r.split("\r\n")
                                if l.lower().startswith("call-id:")), "x")
                    msg = (f"BYE sip:softblue@127.0.0.1 SIP/2.0\r\n"
                           f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bKbye\r\n"
                           f"From: <sip:uas@127.0.0.1>;tag=uastag\r\n"
                           f"To: <sip:u@127.0.0.1>;tag=x\r\n"
                           f"Call-ID: {cid}\r\nCSeq: 1 BYE\r\nContent-Length: 0\r\n\r\n")
                    self.sip.sendto(msg.encode(), a)
                _t.Thread(target=_bye, daemon=True).start()
                continue
            self._reply(req, addr, 200, "OK")


def test_far_end_hangup_stops_listening_early():
    """Otherwise every recording is padded with a random amount of dead air,
    which corrupts any comparison between calls."""
    s = HangupUAS(after=0.6).start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="u",
                          password="", register=False)
        call = SipCall(acct, timeout=5.0)
        with call:
            call.dial("1234")
            t0 = time.monotonic()
            call.listen(10.0)          # would block the full 10s before the fix
            elapsed = time.monotonic() - t0
            assert call.remote_hangup is True
            assert elapsed < 4.0, f"listened {elapsed:.1f}s after hangup"
    finally:
        s.stop()


def test_no_bye_sent_after_the_far_end_hung_up():
    """Sending BYE on a dead dialog just draws a 481."""
    s = HangupUAS(after=0.3).start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="u",
                          password="", register=False)
        call = SipCall(acct, timeout=5.0)
        call.dial("1234")
        call.listen(3.0)
        assert call.remote_hangup
        call.hangup()
        assert call.connected is False
    finally:
        s.stop()


# ---- session reuse (registration churn) --------------------------------------


class CountingUAS(FakeUAS):
    """Counts REGISTERs and can send qualify OPTIONS at the client."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.registers = 0
        self.register_contacts = []
        self.register_expires = []
        self.options_answered = 0
        self._peer = None

    def _sip_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sip.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            req = data.decode(errors="replace")
            if req.startswith("SIP/2.0"):
                if " 200 " in req.split("\r\n")[0]:
                    self.options_answered += 1
                continue
            method = req.split(None, 1)[0]
            self._peer = addr
            if method == "ACK":
                continue
            has_auth = "authorization:" in req.lower()
            if method == "REGISTER":
                if self.require_auth and not has_auth:
                    self._reply(req, addr, 401, "Unauthorized",
                                'WWW-Authenticate: Digest realm="asterisk", '
                                'nonce="deadbeef", qop="auth"\r\n')
                    continue
                self.registers += 1
                for line in req.split("\r\n"):
                    low = line.lower()
                    if low.startswith("contact:"):
                        self.register_contacts.append(line.split(":", 1)[1].strip())
                    elif low.startswith("expires:"):
                        self.register_expires.append(int(line.split(":", 1)[1].strip()))
                self._reply(req, addr, 200, "OK")
                continue
            if method == "INVITE":
                if self.require_auth and not has_auth:
                    self._reply(req, addr, 401, "Unauthorized",
                                'WWW-Authenticate: Digest realm="asterisk", '
                                'nonce="deadbeef", qop="auth"\r\n')
                    continue
                sdp = (f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
                       f"c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                       f"m=audio {self.rtp_port} RTP/AVP {self.codec}\r\n")
                self._reply(req, addr, 200, "OK", body=sdp)
                continue
            self._reply(req, addr, 200, "OK")

    def send_options(self):
        """Qualify the client the way Asterisk does between calls."""
        if not self._peer:
            return
        msg = ("OPTIONS sip:softblue@127.0.0.1 SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bKopt\r\n"
               "From: <sip:uas@127.0.0.1>;tag=q\r\nTo: <sip:u@127.0.0.1>\r\n"
               "Call-ID: qualify\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n")
        self.sip.sendto(msg.encode(), self._peer)


def test_one_registration_serves_many_calls():
    """Registering per call churned the AOR and evicted the previous contact."""
    s = CountingUAS().start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="4343",
                          password="secret")
        from softblue.sipcall import SipSession
        with SipSession(acct, timeout=5.0) as sess:
            for _ in range(3):
                call = SipCall(acct, timeout=5.0, session=sess)
                with call:
                    call.dial("2195002600")
                    assert call.connected
        time.sleep(0.2)
        # One registration for three calls, plus the Expires:0 on close.
        assert s.registers == 2, f"expected register+unregister, got {s.registers}"
        assert s.register_expires[-1] == 0, "session should unregister on close"
        # Every call reused the same contact, so nothing was evicted.
        assert len(set(s.register_contacts)) == 1, s.register_contacts
    finally:
        s.stop()


def test_per_call_sessions_still_work_standalone():
    """Omitting the session keeps single-shot use a one-liner."""
    s = CountingUAS().start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="4343",
                          password="secret")
        call = SipCall(acct, timeout=5.0)
        with call:
            call.dial("1234")
            assert call.connected
        assert s.registers >= 1
    finally:
        s.stop()


def test_qualify_options_answered_between_calls():
    """Unanswered OPTIONS is what made the contact go Unreachable."""
    s = CountingUAS().start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="4343",
                          password="secret")
        from softblue.sipcall import SipSession
        with SipSession(acct, timeout=5.0) as sess:
            before = s.options_answered
            for _ in range(3):
                s.send_options()          # no call in progress
                time.sleep(0.15)
            assert s.options_answered - before >= 3, "OPTIONS went unanswered"
    finally:
        s.stop()


def test_shared_session_survives_a_failed_call():
    """One bad extension must not tear down the registration mid-sweep."""
    s = CountingUAS().start()
    try:
        acct = SipAccount(host="127.0.0.1", port=s.port, user="4343",
                          password="secret")
        from softblue.sipcall import SipSession
        with SipSession(acct, timeout=5.0) as sess:
            bad = SipCall(acct, timeout=5.0, session=sess)
            with pytest.raises(SipError):
                bad.dial("1234 SIP/2.0\r\nX-Evil: 1")   # rejected before sending
            bad.hangup()
            good = SipCall(acct, timeout=5.0, session=sess)
            with good:
                good.dial("2195002600")
                assert good.connected
            assert sess.registered
    finally:
        s.stop()
