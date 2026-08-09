"""Minimal SIP/RTP transport — place a call and inject generated tones into it.

Scope is deliberately narrow, covering exactly what the PhreakMe PBX offers
(``asterisk-config/pjsip.conf``): SIP over UDP, digest auth, and G.711 µ-law /
A-law media. No TCP, no TLS, no SRTP, no video, no re-INVITE handling.

Why this exists rather than shelling out to a softphone: injecting audio through
a speaker into a handset destroys the two properties the PhreakMe coin scheme
depends on — absolute level (nickel and dime differ only by 3 dB) and clean tone
edges. An RTP stream preserves both exactly. It also lets us *record* the far
end, which is what turns a black-box challenge into a readable one.

Nothing here logs or echoes the account password.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

import numpy as np

from . import g711

log = logging.getLogger(__name__)

RTP_SAMPLE_RATE = 8000          # G.711 is 8 kHz by definition
PTIME_S = 0.020                 # 20 ms packets — 160 samples
SAMPLES_PER_PACKET = int(RTP_SAMPLE_RATE * PTIME_S)

# RFC 4733 named telephone events.
DTMF_EVENTS = {**{str(d): d for d in range(10)},
               "*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15}


class SipError(RuntimeError):
    pass


# A SIP message is CRLF-delimited text, so any user-supplied value spliced into
# a header or the request line must be constrained or it can inject headers.
_SIP_TOKEN_OK = re.compile(r"^[A-Za-z0-9*#+._@!$%&'~()-]{1,128}$")


def sip_token(value: str, field: str) -> str:
    """Validate a value destined for a SIP request line or header.

    Rejects CR, LF, spaces, angle brackets, commas, semicolons and colons —
    everything that could terminate a header or start a new one. Without this an
    ``extension`` of ``1234 SIP/2.0\\r\\nRoute: <sip:evil>`` would splice
    attacker-chosen headers into the outbound INVITE.
    """
    text = "" if value is None else str(value)
    if not _SIP_TOKEN_OK.match(text):
        bad = next((repr(c) for c in text if not re.match(r"[A-Za-z0-9*#+._@!$%&'~()-]", c)),
                   None)
        raise SipError(
            f"invalid {field} {text[:40]!r}"
            + (f": {bad} is not allowed" if bad else ": must be 1-128 characters")
            + ". Use digits, letters, and * # . _ - only.")
    return text


# ---- account / config --------------------------------------------------------


@dataclass
class SipAccount:
    """Connection details. ``password`` is never logged or echoed."""

    host: str
    port: int = 5060
    user: str = "softphone"
    password: str = ""
    domain: str | None = None
    local_port: int = 0             # 0 = ephemeral
    rtp_port: int = 0
    register: bool = True
    codecs: tuple[int, ...] = (g711.PCMU, g711.PCMA)

    def __post_init__(self):
        if not self.domain:
            self.domain = self.host
        # These are interpolated into the request line and From/To/Contact.
        sip_token(self.host, "host")
        sip_token(self.user, "user")
        sip_token(self.domain, "domain")

    def __repr__(self) -> str:      # keep secrets out of tracebacks
        return (f"SipAccount(host={self.host!r}, port={self.port}, "
                f"user={self.user!r}, password=<redacted>)")

    def public(self) -> dict:
        """Everything except the secret — safe to hand to a UI."""
        return {"host": self.host, "port": self.port, "user": self.user,
                "domain": self.domain, "register": self.register,
                "has_password": bool(self.password)}


def load_account(host: str | None = None, port: int | None = None,
                 user: str | None = None, no_register: bool = False) -> SipAccount:
    """Build an account from flags, ``~/.softblue/sip.yaml`` and the environment.

    Precedence is flag > environment > file. The password is read only from
    ``$SOFTBLUE_SIP_PASSWORD`` or the file — never a flag, which would leak it
    into shell history and the process table.
    """
    import os

    import yaml

    from .config import config_dir

    data: dict = {}
    path = config_dir() / "sip.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            # Never echo the parser's message: a YAML error quotes the offending
            # source line verbatim, so a malformed `password:` line would put the
            # plaintext secret into this string — and thence into an
            # unauthenticated /api/sip/status response. Position only.
            mark = getattr(e, "problem_mark", None)
            where = (f" (line {mark.line + 1}, column {mark.column + 1})"
                     if mark is not None else "")
            raise SipError(
                f"{path} is not valid YAML{where}. Detail withheld because the "
                f"file holds a password — open the file to see the error. "
                f"Common causes: tab characters, or an unquoted value starting "
                f"with @ ` % * & or !.")
        if not isinstance(data, dict):
            raise SipError(f"{path} must contain a mapping of settings")

    host = host or os.environ.get("SOFTBLUE_SIP_HOST") or data.get("host")
    if not host:
        raise SipError(
            f"No PBX host configured. Set 'host:' in {path}, export "
            "$SOFTBLUE_SIP_HOST, or pass --host.")

    # YAML infers types, so a bare numeric `user: 4242` arrives as an int and a
    # bare `password: 007` is read as a number (in YAML 1.1, even as octal) —
    # which would authenticate with a value the user never wrote. We cannot undo
    # that here, so coerce for safety and warn loudly enough to be actionable.
    for key in ("user", "password"):
        if key in data and not isinstance(data[key], str):
            log.warning(
                "sip.yaml: %r is not quoted, so YAML parsed it as %s. Quote it "
                "(%s: \"...\") — leading zeros are otherwise lost or read as octal.",
                key, type(data[key]).__name__, key)

    def _s(v, default=""):
        return default if v is None else str(v)

    raw_port = port or os.environ.get("SOFTBLUE_SIP_PORT", 0) or data.get("port", 5060)
    try:
        parsed_port = int(raw_port)
    except (TypeError, ValueError):
        raise SipError(f"port must be a number, got {raw_port!r} (check {path})")
    if not 1 <= parsed_port <= 65535:
        raise SipError(f"port must be 1-65535, got {parsed_port}")

    return SipAccount(
        host=_s(host),
        port=parsed_port,
        user=_s(user or os.environ.get("SOFTBLUE_SIP_USER")
                or data.get("user"), "softphone"),
        password=_s(os.environ.get("SOFTBLUE_SIP_PASSWORD")
                    or data.get("password")),
        domain=_s(data.get("domain")) or None,
        register=(not no_register) and bool(data.get("register", True)),
    )


# ---- digest auth -------------------------------------------------------------

_AUTH_PARAM = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def parse_auth_header(value: str) -> dict[str, str]:
    """Parse a WWW-Authenticate / Proxy-Authenticate value into its params."""
    out: dict[str, str] = {}
    for m in _AUTH_PARAM.finditer(value):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def digest_response(user: str, password: str, realm: str, nonce: str,
                    method: str, uri: str, qop: str | None = None,
                    nc: str = "00000001", cnonce: str | None = None) -> str:
    """RFC 2617 digest. Supports both the qop and legacy (no-qop) forms."""
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        return _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def build_auth_header(account: SipAccount, params: dict[str, str],
                      method: str, uri: str, proxy: bool = False) -> str:
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop")
    if qop:
        # A server may offer several; we only implement plain "auth".
        qop = "auth" if "auth" in [q.strip() for q in qop.split(",")] else None
    cnonce = uuid.uuid4().hex[:16] if qop else None
    resp = digest_response(account.user, account.password, realm, nonce,
                           method, uri, qop, "00000001", cnonce)
    parts = [f'username="{account.user}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"', f'response="{resp}"']
    if params.get("opaque"):
        parts.append(f'opaque="{params["opaque"]}"')
    if params.get("algorithm"):
        parts.append(f'algorithm={params["algorithm"]}')
    if qop:
        parts += [f"qop={qop}", "nc=00000001", f'cnonce="{cnonce}"']
    return "Digest " + ", ".join(parts)


# ---- SIP message plumbing ----------------------------------------------------


@dataclass
class SipResponse:
    code: int
    reason: str
    headers: dict[str, str]
    body: str

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def diagnosis(self) -> str:
        """Any header the far end used to explain a failure.

        Asterisk puts the real reason in Warning/Reason rather than the status
        line, so "503 Service Unavailable" alone is rarely actionable.
        """
        bits = []
        for h in ("warning", "reason", "retry-after", "x-asterisk-hangupcausecode",
                  "x-asterisk-hangupcause"):
            v = self.header(h)
            if v:
                bits.append(f"{h}: {v}")
        return "; ".join(bits)


def parse_response(data: bytes) -> SipResponse | None:
    try:
        text = data.decode(errors="replace")
    except Exception:
        return None
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    if not lines or not lines[0].startswith("SIP/2.0"):
        return None
    parts = lines[0].split(None, 2)
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    reason = parts[2] if len(parts) > 2 else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        k = k.strip().lower()
        if not k:
            continue
        # Multiple Via/Record-Route headers: keep the first, which is ours.
        headers.setdefault(k, v.strip())
    return SipResponse(code, reason, headers, body)


def build_sdp(local_ip: str, rtp_port: int, codecs) -> str:
    """Offer only the codecs we can actually encode, plus RFC4733 DTMF."""
    rtpmap = {g711.PCMU: "PCMU/8000", g711.PCMA: "PCMA/8000"}
    pts = " ".join(str(c) for c in codecs)
    lines = [
        "v=0",
        f"o=softblue {random.randint(1, 2**31)} 1 IN IP4 {local_ip}",
        "s=softblue",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {rtp_port} RTP/AVP {pts} 101",
    ]
    lines += [f"a=rtpmap:{c} {rtpmap[c]}" for c in codecs]
    lines += ["a=rtpmap:101 telephone-event/8000", "a=fmtp:101 0-16",
              f"a=ptime:{int(PTIME_S * 1000)}", "a=sendrecv"]
    return "\r\n".join(lines) + "\r\n"


def parse_sdp(sdp: str) -> tuple[str | None, int | None, int | None]:
    """Pull (remote_ip, remote_rtp_port, payload_type) out of an SDP answer."""
    ip, port, pt, _ = parse_sdp_full(sdp)
    return ip, port, pt


def parse_sdp_full(sdp: str) -> tuple[str | None, int | None, int | None, int | None]:
    """As :func:`parse_sdp`, plus the negotiated telephone-event payload type.

    The DTMF payload type is dynamic, so it must be read from the answer's
    rtpmap rather than assumed to be the 101 we offered.
    """
    ip = port = pt = tel = None
    offered: list[int] = []
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line[9:].split("/")[0].strip()
        elif line.startswith("m=audio "):
            f = line.split()
            if len(f) >= 4:
                port = int(f[1])
                offered = [int(t) for t in f[3:] if t.isdigit()]
                for p in offered:
                    if p in g711.CODECS:
                        pt = p
                        break
        elif line.lower().startswith("a=rtpmap:"):
            body = line[9:]
            num, _, name = body.partition(" ")
            if num.strip().isdigit() and name.strip().lower().startswith(
                    "telephone-event"):
                tel = int(num.strip())
    # Some answers list the payload type in m= but omit the rtpmap.
    if tel is None and 101 in offered:
        tel = 101
    return ip, port, pt, tel


def local_ip_towards(host: str, port: int) -> str:
    """Source address the OS would pick for this destination."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---- RTP ---------------------------------------------------------------------


class RtpSession:
    """Bidirectional RTP over one UDP socket."""

    def __init__(self, local_port: int = 0, payload_type: int = g711.PCMU,
                 telephone_event_pt: int | None = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", local_port))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.payload_type = payload_type
        self.telephone_event_pt = telephone_event_pt
        self.ssrc = random.randint(0, 2**32 - 1)
        self.seq = random.randint(0, 0xFFFF)
        self.timestamp = random.randint(0, 2**31)
        self.remote: tuple[str, int] | None = None
        self._rx: list[np.ndarray] = []
        self._rx_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_receiving(self) -> None:
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if len(data) < 12:
                continue
            b0, b1 = data[0], data[1]
            if (b0 >> 6) != 2:          # not RTP v2
                continue
            cc = b0 & 0x0F
            offset = 12 + cc * 4
            if b0 & 0x10:               # extension header
                if len(data) < offset + 4:
                    continue
                ext_len = int.from_bytes(data[offset + 2:offset + 4], "big")
                offset += 4 + ext_len * 4
            pt = b1 & 0x7F
            if pt not in g711.CODECS or len(data) <= offset:
                continue
            samples = g711.decode(data[offset:], pt)
            with self._rx_lock:
                self._rx.append(samples)

    def send(self, pcm16: np.ndarray) -> None:
        """Send int16 PCM as paced 20 ms RTP packets."""
        if not self.remote:
            raise SipError("no remote RTP address negotiated")
        n = len(pcm16)
        pad = (-n) % SAMPLES_PER_PACKET
        if pad:
            pcm16 = np.concatenate([pcm16, np.zeros(pad, dtype=np.int16)])
        start = time.monotonic()
        for i in range(0, len(pcm16), SAMPLES_PER_PACKET):
            chunk = pcm16[i:i + SAMPLES_PER_PACKET]
            self._send_raw(self._packet(g711.encode(chunk, self.payload_type),
                                        self.payload_type, self.timestamp))
            self.seq = (self.seq + 1) & 0xFFFF
            self.timestamp = (self.timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF
            # Pace against a fixed origin so we do not drift late over a long send.
            target = start + (i // SAMPLES_PER_PACKET + 1) * PTIME_S
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def send_silence(self, seconds: float) -> None:
        self.send(np.zeros(int(RTP_SAMPLE_RATE * seconds), dtype=np.int16))

    def _packet(self, payload: bytes, payload_type: int, timestamp: int,
                marker: bool = False) -> bytes:
        b1 = (payload_type & 0x7F) | (0x80 if marker else 0)
        return (bytes([0x80, b1])
                + self.seq.to_bytes(2, "big")
                + (timestamp & 0xFFFFFFFF).to_bytes(4, "big")
                + self.ssrc.to_bytes(4, "big")
                + payload)

    def send_dtmf(self, digits: str, duration: float = 0.12,
                  gap: float = 0.08, volume: int = 10) -> None:
        """Send digits as RFC 4733 named telephone events.

        Asterisk with ``dtmf_mode = rfc4733`` listens for these RTP events and
        ignores in-band DTMF audio entirely, so playing touch-tones as audio
        would be silently dropped by the dialplan.

        Per RFC 4733 the timestamp stays pinned at the event's start for every
        packet of that event while ``duration`` counts up; the end of the event
        is signalled by three retransmissions with the E bit set.
        """
        if self.telephone_event_pt is None:
            raise SipError(
                "the PBX did not negotiate a telephone-event payload type, so "
                "RFC 4733 DTMF cannot be sent")
        if not self.remote:
            raise SipError("no remote RTP address negotiated")

        for digit in digits:
            if digit in " -":
                continue
            event = DTMF_EVENTS.get(digit.upper())
            if event is None:
                raise SipError(f"{digit!r} is not a DTMF digit")

            start_ts = self.timestamp
            n_packets = max(1, int(duration / PTIME_S))
            for i in range(n_packets):
                samples = (i + 1) * SAMPLES_PER_PACKET
                payload = bytes([event, volume & 0x3F]) + \
                    min(samples, 0xFFFF).to_bytes(2, "big")
                self._send_raw(self._packet(payload, self.telephone_event_pt,
                                            start_ts, marker=(i == 0)))
                self.seq = (self.seq + 1) & 0xFFFF
                time.sleep(PTIME_S)

            total = n_packets * SAMPLES_PER_PACKET
            end_payload = bytes([event, (volume & 0x3F) | 0x80]) + \
                min(total, 0xFFFF).to_bytes(2, "big")
            for _ in range(3):      # RFC 4733 §2.5.1.2: send the end three times
                self._send_raw(self._packet(end_payload,
                                            self.telephone_event_pt, start_ts))
                self.seq = (self.seq + 1) & 0xFFFF

            # Advance the clock past the event so following audio stays in step.
            self.timestamp = (self.timestamp + total) & 0xFFFFFFFF
            if gap > 0:
                self.send_silence(gap)

    def _send_raw(self, packet: bytes) -> None:
        try:
            self.sock.sendto(packet, self.remote)
        except OSError as e:
            raise SipError(f"RTP send failed: {e}")

    def received(self) -> np.ndarray:
        with self._rx_lock:
            if not self._rx:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(self._rx)

    def clear_received(self) -> None:
        with self._rx_lock:
            self._rx.clear()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self.sock.close()


# ---- the call ----------------------------------------------------------------


class SipSession:
    """One SIP socket and one registration, shared by every call on it.

    Registering per call was churning the AOR: each call bound a fresh ephemeral
    port, so every REGISTER evicted the previous contact
    (``Removed contact ... due to remove existing``) and a sweep of N calls meant
    N registrations. Worse, nothing answered the PBX's qualify OPTIONS between
    calls, so the contact kept going ``Unreachable``.

    A single reader thread demultiplexes the socket — responses go to whoever is
    awaiting them, OPTIONS are answered continuously so the contact stays up, and
    an inbound BYE is routed to the active call. One reader means no two threads
    ever race on ``recvfrom``.
    """

    MAX_QUEUED = 64          # bound the response backlog; stale ones are dropped

    def __init__(self, account: SipAccount, timeout: float = 30.0,
                 expires: int = 300):
        self.account = account
        self.timeout = timeout
        self.expires = expires
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", account.local_port))
        self.sock.settimeout(0.2)
        self.local_port = self.sock.getsockname()[1]
        self.local_ip = local_ip_towards(account.host, account.port)
        self.registered = False
        self.active_call = None

        self.call_id = f"{uuid.uuid4().hex}@{self.local_ip}"   # for REGISTER
        self.from_tag = uuid.uuid4().hex[:12]
        self.cseq = 0

        self._responses: list[SipResponse] = []
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._refresher: threading.Thread | None = None

    # -- socket plumbing --

    def send(self, raw: bytes) -> None:
        self.sock.sendto(raw, (self.account.host, self.account.port))

    def _reply(self, req: str, addr, code: int, reason: str) -> None:
        def hdr(name):
            for line in req.split("\r\n"):
                if line.lower().startswith(name):
                    return line.split(":", 1)[1].strip()
            return ""
        msg = (f"SIP/2.0 {code} {reason}\r\n"
               f"Via: {hdr('via:')}\r\nFrom: {hdr('from:')}\r\nTo: {hdr('to:')}\r\n"
               f"Call-ID: {hdr('call-id:')}\r\nCSeq: {hdr('cseq:')}\r\n"
               f"Content-Length: 0\r\n\r\n")
        try:
            self.sock.sendto(msg.encode(), addr)
        except OSError:
            pass

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            try:
                text = data.decode(errors="replace")
            except Exception:
                continue

            if text.startswith("SIP/2.0"):
                resp = parse_response(data)
                if resp is not None:
                    with self._cv:
                        self._responses.append(resp)
                        del self._responses[:-self.MAX_QUEUED]
                        self._cv.notify_all()
                continue

            method = text.split(None, 1)[0] if text.split() else ""
            if method == "OPTIONS":
                # Answer the qualify ping even between calls, or Asterisk marks
                # the contact Unreachable and may stop routing to it.
                self._reply(text, addr, 200, "OK")
            elif method in ("BYE", "CANCEL"):
                self._reply(text, addr, 200, "OK")
                call = self.active_call
                if call is not None:
                    call.remote_hangup = True
                    log.info("far end hung up (%s)", method)
            elif method == "ACK":
                pass
            elif method:
                self._reply(text, addr, 200, "OK")

    def await_response(self, timeout: float | None = None,
                       branch: str | None = None, cseq: int | None = None,
                       want_final: bool = True) -> SipResponse:
        """Wait for a response belonging to this transaction.

        Matching on branch/CSeq matters: without it a retransmitted challenge
        from an earlier transaction gets read as the answer to the current one.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        seen_any = False
        with self._cv:
            while True:
                for i, resp in enumerate(list(self._responses)):
                    if branch and f"branch={branch}" not in resp.header("via"):
                        continue
                    got = resp.header("cseq").split()
                    if cseq is not None and got and got[0].isdigit() \
                            and int(got[0]) != cseq:
                        continue
                    del self._responses[i]
                    seen_any = True
                    if resp.code >= 200 or not want_final:
                        return resp
                    log.debug("provisional %s %s", resp.code, resp.reason)
                    break
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SipError(
                            f"timed out after {self.timeout:.0f}s waiting for a "
                            "SIP response"
                            + ("" if seen_any else " (nothing received)"))
                    self._cv.wait(min(remaining, 0.2))

    # -- registration --

    def _branch(self) -> str:
        return "z9hG4bK" + uuid.uuid4().hex[:16]

    def _register_request(self, expires: int, auth: dict | None = None) -> bytes:
        a = self.account
        self.cseq += 1
        uri = f"sip:{a.domain}"
        headers = {
            "Via": f"SIP/2.0/UDP {self.local_ip}:{self.local_port};"
                   f"branch={self._last_branch};rport",
            "Max-Forwards": "70",
            "From": f"<sip:{a.user}@{a.domain}>;tag={self.from_tag}",
            "To": f"<sip:{a.user}@{a.domain}>",
            "Call-ID": self.call_id,
            "CSeq": f"{self.cseq} REGISTER",
            "Contact": f"<sip:{a.user}@{self.local_ip}:{self.local_port}>",
            "Expires": str(expires),
            "User-Agent": "softblue",
            "Content-Length": "0",
        }
        headers.update(auth or {})
        lines = [f"REGISTER {uri} SIP/2.0"] + \
            [f"{k}: {v}" for k, v in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def register(self, expires: int | None = None) -> None:
        """REGISTER once. Safe to call again to refresh the same binding."""
        expires = self.expires if expires is None else expires
        a = self.account
        uri = f"sip:{a.domain}"

        self._last_branch = self._branch()
        self.send(self._register_request(expires))
        resp = self.await_response(branch=self._last_branch, cseq=self.cseq)

        if resp.code in (401, 407):
            hdr = resp.header("proxy-authenticate" if resp.code == 407
                              else "www-authenticate")
            if not hdr:
                raise SipError(f"{resp.code} {resp.reason} without an auth challenge")
            if not a.password:
                raise SipError(
                    f"{resp.code} {resp.reason}: the PBX wants credentials but no "
                    "password is configured (set SOFTBLUE_SIP_PASSWORD)")
            field = "Proxy-Authorization" if resp.code == 407 else "Authorization"
            params = parse_auth_header(hdr)
            self._last_branch = self._branch()
            self.send(self._register_request(
                expires, {field: build_auth_header(a, params, "REGISTER", uri)}))
            resp = self.await_response(branch=self._last_branch, cseq=self.cseq)

        if resp.code != 200:
            raise SipError(f"REGISTER failed: {resp.code} {resp.reason}")
        self.registered = expires > 0
        if expires > 0:
            log.info("registered as %s (expires %ss, contact %s:%s)",
                     a.user, expires, self.local_ip, self.local_port)
            self._start_refresh(expires)

    def _start_refresh(self, expires: int) -> None:
        if self._refresher is not None:
            return

        def _loop():
            # Refresh at half the expiry so the binding never lapses mid-sweep.
            while not self._stop.wait(max(30.0, expires / 2.0)):
                try:
                    self.register(expires)
                except SipError as e:
                    log.warning("registration refresh failed: %s", e)
                    return

        self._refresher = threading.Thread(target=_loop, daemon=True)
        self._refresher.start()

    def unregister(self) -> None:
        """Drop the binding cleanly (Expires: 0) instead of leaving it to rot."""
        if not self.registered:
            return
        try:
            self.register(expires=0)
        except SipError as e:
            log.debug("unregister failed: %s", e)
        self.registered = False

    def close(self) -> None:
        self.unregister()
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=1.5)
        self.sock.close()

    def __enter__(self) -> "SipSession":
        if self.account.register:
            self.register()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SipCall:
    """One outbound call. Usable as a context manager.

    Pass ``session`` to reuse an existing registration across several calls
    (what ``softblue redbox sweep`` does); omit it and the call creates and owns
    a private session, which keeps single-shot use a one-liner.
    """

    def __init__(self, account: SipAccount, timeout: float = 30.0,
                 session: "SipSession | None" = None):
        self.account = account
        self.timeout = timeout
        self._owns_session = session is None
        self.session = session or SipSession(account, timeout)
        self.sock = self.session.sock
        self.local_port = self.session.local_port
        self.local_ip = self.session.local_ip
        self.rtp = RtpSession(account.rtp_port)
        self.call_id = f"{uuid.uuid4().hex}@{self.local_ip}"
        self.from_tag = uuid.uuid4().hex[:12]
        self.to_tag = ""
        self.cseq = 0
        self.connected = False
        self._target = ""
        self._route: str = ""
        self._last_branch: str | None = None
        # Set by the session reader on inbound BYE. Without it we keep
        # "listening" to a dead call, and the trailing silence pollutes any
        # comparison between one call's audio and another's.
        self.remote_hangup = False

    # -- low-level send/recv --

    def _addr(self) -> tuple[str, int]:
        return (self.account.host, self.account.port)

    def _branch(self) -> str:
        return "z9hG4bK" + uuid.uuid4().hex[:16]

    def _request(self, method: str, uri: str, extra: dict[str, str] | None = None,
                 body: str = "", branch: str | None = None,
                 to_hdr: str | None = None) -> bytes:
        self.cseq += 1 if method != "ACK" else 0
        a = self.account
        to = to_hdr or f"<{uri}>"
        headers = {
            "Via": f"SIP/2.0/UDP {self.local_ip}:{self.local_port};"
                   f"branch={branch or self._branch()};rport",
            "Max-Forwards": "70",
            "From": f"<sip:{a.user}@{a.domain}>;tag={self.from_tag}",
            "To": to,
            "Call-ID": self.call_id,
            "CSeq": f"{self.cseq} {method}",
            "Contact": f"<sip:{a.user}@{self.local_ip}:{self.local_port}>",
            "User-Agent": "softblue",
            "Content-Length": str(len(body)),
        }
        if body:
            headers["Content-Type"] = "application/sdp"
        headers.update(extra or {})
        lines = [f"{method} {uri} SIP/2.0"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        msg = "\r\n".join(lines) + "\r\n\r\n" + body
        return msg.encode()

    def _send(self, raw: bytes) -> None:
        self.session.send(raw)

    def _await(self, want_final: bool = True, branch: str | None = None,
               cseq: int | None = None) -> SipResponse:
        """Delegate to the session's reader; it owns the only recvfrom loop."""
        return self.session.await_response(timeout=self.timeout, branch=branch,
                                           cseq=cseq, want_final=want_final)

    def _ack_failure(self, uri: str, resp: SipResponse, branch: str) -> None:
        """ACK a non-2xx final response (RFC 3261 §17.1.1.3).

        Mandatory, and unlike the 2xx ACK it belongs to the *same* client
        transaction, so it must reuse the INVITE's branch and CSeq. Skipping it
        leaves the server transaction retransmitting the failure on timer G for
        up to 32 s.
        """
        to = resp.header("to") or f"<{uri}>"
        try:
            self._send(self._request("ACK", uri, branch=branch,
                                     to_hdr=to.strip()))
        except OSError as e:
            log.debug("could not ACK %s: %s", resp.code, e)

    def _with_auth(self, method: str, uri: str, resp: SipResponse,
                   body: str = "", to_hdr: str | None = None) -> SipResponse:
        """Re-send a request carrying digest credentials."""
        proxy = resp.code == 407
        hdr = resp.header("proxy-authenticate" if proxy else "www-authenticate")
        if not hdr:
            raise SipError(f"{resp.code} {resp.reason} without an auth challenge")
        if not self.account.password:
            raise SipError(
                f"{resp.code} {resp.reason}: the PBX wants credentials but no "
                "password is configured (set SOFTBLUE_SIP_PASSWORD)")
        params = parse_auth_header(hdr)
        field_name = "Proxy-Authorization" if proxy else "Authorization"
        auth = build_auth_header(self.account, params, method, uri, proxy)
        branch = self._branch()
        self._last_branch = branch
        self._send(self._request(method, uri, {field_name: auth}, body,
                                 branch=branch, to_hdr=to_hdr))
        return self._await(branch=branch, cseq=self.cseq)

    # -- call flow --

    def register(self) -> None:
        """Kept for compatibility; registration lives on the session."""
        self.session.register()

    def dial(self, extension: str) -> None:
        a = self.account
        extension = sip_token(extension, "extension")
        uri = f"sip:{extension}@{a.domain}"
        self._target = uri
        sdp = build_sdp(self.local_ip, self.rtp.port, a.codecs)
        branch = self._branch()
        self._send(self._request("INVITE", uri, body=sdp, branch=branch))
        resp = self._await(branch=branch, cseq=self.cseq)
        if resp.code in (401, 407):
            # ACK the challenge before retrying, or the PBX keeps resending it.
            self._ack_failure(uri, resp, branch)
            resp = self._with_auth("INVITE", uri, resp, body=sdp)
        if resp.code != 200:
            self._ack_failure(uri, resp, self._last_branch or branch)
            why = resp.diagnosis()
            raise SipError(f"INVITE failed: {resp.code} {resp.reason}"
                           + (f" [{why}]" if why else ""))

        to = resp.header("to")
        m = re.search(r"tag=([^;\s]+)", to)
        self.to_tag = m.group(1) if m else ""
        contact = resp.header("contact")
        cm = re.search(r"<([^>]+)>", contact)
        self._route = cm.group(1) if cm else uri

        ip, port, pt, tel_pt = parse_sdp_full(resp.body)
        if not ip or not port:
            raise SipError("200 OK carried no usable SDP answer")
        if pt is None:
            raise SipError(
                "PBX chose a codec we cannot encode — only G.711 ulaw/alaw are "
                "supported (and only those preserve coin-tone levels)")
        self.rtp.remote = (ip, port)
        self.rtp.payload_type = pt
        self.rtp.telephone_event_pt = tel_pt
        self.rtp.start_receiving()

        # ACK goes to the dialog target and reuses the INVITE's CSeq.
        self._send(self._request("ACK", self._route,
                                 to_hdr=f"<{uri}>;tag={self.to_tag}"))
        self.connected = True
        log.info("connected to %s, codec %s, rtp %s:%s, dtmf pt %s",
                 extension, g711.CODECS[pt][0], ip, port,
                 tel_pt if tel_pt is not None else "none")
        # Send a little silence first: a NAT-bound PBX learns our RTP source
        # address from inbound packets (comedia), so it needs to hear us before
        # it will send anything back.
        self.rtp.send_silence(0.2)
        self.session.active_call = self

    def play(self, samples, sample_rate: int = RTP_SAMPLE_RATE) -> None:
        """Send engine float samples (-1..1) into the call."""
        if not self.connected:
            raise SipError("not connected")
        if sample_rate != RTP_SAMPLE_RATE:
            raise SipError(
                f"G.711 is {RTP_SAMPLE_RATE} Hz; got {sample_rate}. Generate at "
                f"8000 (-r 8000) rather than resampling, which would round the "
                f"coin-tone edges.")
        self.rtp.send(g711.float_to_pcm16(np.asarray(samples)))

    def dtmf(self, digits: str, duration: float = 0.12,
             gap: float = 0.08) -> None:
        """Send DTMF as RFC 4733 events (what dtmf_mode=rfc4733 listens for)."""
        if not self.connected:
            raise SipError("not connected")
        self.rtp.send_dtmf(digits, duration=duration, gap=gap)

    def run_steps(self, steps, render_coins, on_step=None) -> list[dict]:
        """Execute parsed dial-string steps against this call.

        ``render_coins`` maps a coin-symbol string to float samples; injecting it
        keeps this module free of any dependency on the tone engine. Returns a
        timeline of what ran and when, so a caller can line the far-end
        recording up against its own actions.
        """
        if not self.connected:
            raise SipError("not connected")
        started = time.monotonic()
        timeline: list[dict] = []
        for step in steps:
            at = time.monotonic() - started
            if step.kind == "dtmf":
                self.dtmf(step.value)
            elif step.kind == "wait":
                self.listen(step.seconds)
            elif step.kind == "coins":
                self.play(render_coins(step.value))
            else:
                raise SipError(f"unknown dial-string step {step.kind!r}")
            entry = {"at": round(at, 3), "kind": step.kind,
                     "detail": step.describe()}
            timeline.append(entry)
            if on_step:
                on_step(entry)
        return timeline

    def listen(self, seconds: float) -> np.ndarray:
        """Hold the call open, then return everything received since ``clear()``.

        Deliberately does not clear on entry. A challenge often starts replying
        while our own tones are still going out, and clearing here would discard
        exactly the response we called to capture. Call :meth:`clear` first if
        you genuinely want an empty window.
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.remote_hangup:
                log.info("stopped listening early: far end hung up")
                break
            time.sleep(0.02)
        return self.recorded()

    def clear(self) -> None:
        """Drop audio received so far, to start a fresh capture window."""
        self.rtp.clear_received()

    def recorded(self) -> np.ndarray:
        return g711.pcm16_to_float(self.rtp.received())

    def hangup(self) -> None:
        if self.session.active_call is self:
            self.session.active_call = None
        if self.connected and self.remote_hangup:
            # They already tore the dialog down; a BYE now would draw a 481.
            self.connected = False
        if self.connected:
            try:
                self._send(self._request(
                    "BYE", self._route,
                    to_hdr=f"<{self._target}>;tag={self.to_tag}"))
                self._await()
            except (SipError, OSError):
                pass                    # teardown is best-effort
            self.connected = False
        self.rtp.close()
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> "SipCall":
        # A shared session has already registered; only register if we own it.
        if self._owns_session and self.account.register:
            self.session.register()
        return self

    def __exit__(self, *exc) -> None:
        self.hangup()
