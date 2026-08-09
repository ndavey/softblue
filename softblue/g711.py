"""G.711 µ-law / A-law codecs (numpy, no stdlib ``audioop``).

``audioop`` was removed in Python 3.13, so these are implemented directly from
the ITU-T G.711 companding tables. Both are the only codecs the PhreakMe PBX
offers (``allow = ulaw`` / ``alaw``), which is fortunate: they are plain 8-bit
companding with no frame model, so a 60 ms coin tone survives them intact.
Anything transform-based (Opus, GSM, G.729) would smear the tone edges that the
coin detector keys on.
"""

from __future__ import annotations

import numpy as np

PCMU = 0   # RTP payload type for µ-law
PCMA = 8   # RTP payload type for A-law

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635
_ALAW_CLIP = 32635

# Segment boundaries: searchsorted(..., 'right') yields the segment index.
_ULAW_SEG = np.array([256, 512, 1024, 2048, 4096, 8192, 16384, 32768], dtype=np.int32)
_ALAW_SEG = np.array([0x20, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800, 0x1000],
                     dtype=np.int32)


def ulaw_encode(pcm: np.ndarray) -> np.ndarray:
    """int16 linear PCM → µ-law bytes."""
    x = np.asarray(pcm, dtype=np.int32)
    sign = (x < 0).astype(np.int32) * 0x80
    mag = np.minimum(np.abs(x), _ULAW_CLIP) + _ULAW_BIAS
    exponent = np.searchsorted(_ULAW_SEG, mag, side="right").astype(np.int32)
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


def ulaw_decode(data) -> np.ndarray:
    """µ-law bytes → int16 linear PCM."""
    u = (~np.frombuffer(bytes(data), dtype=np.uint8).astype(np.int32)) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = (((mantissa << 3) + _ULAW_BIAS) << exponent) - _ULAW_BIAS
    return np.where(sign, -mag, mag).astype(np.int16)


def alaw_encode(pcm: np.ndarray) -> np.ndarray:
    """int16 linear PCM → A-law bytes."""
    x = np.asarray(pcm, dtype=np.int32)
    x = np.clip(x, -_ALAW_CLIP, _ALAW_CLIP) >> 3      # to 13-bit
    neg = x < 0
    val = np.where(neg, -x - 1, x)
    seg = np.searchsorted(_ALAW_SEG, val, side="right").astype(np.int32)
    shift = np.where(seg < 2, 1, seg)
    aval = (np.clip(seg, 0, 7) << 4) | ((val >> shift) & 0x0F)
    aval = np.where(seg >= 8, 0x7F, aval)             # saturate
    mask = np.where(neg, 0x55, 0xD5)
    return ((aval ^ mask) & 0xFF).astype(np.uint8)


def alaw_decode(data) -> np.ndarray:
    """A-law bytes → int16 linear PCM."""
    a = np.frombuffer(bytes(data), dtype=np.uint8).astype(np.int32) ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a >> 4) & 0x07
    t = np.where(seg == 0, t + 8, t + 0x108)
    t = np.where(seg >= 2, t << (np.maximum(seg, 1) - 1), t)
    return np.where(a & 0x80, t, -t).astype(np.int16)


CODECS = {
    PCMU: ("PCMU", ulaw_encode, ulaw_decode),
    PCMA: ("PCMA", alaw_encode, alaw_decode),
}


def encode(pcm: np.ndarray, payload_type: int) -> bytes:
    if payload_type not in CODECS:
        raise ValueError(f"unsupported payload type {payload_type}")
    return CODECS[payload_type][1](pcm).tobytes()


def decode(data, payload_type: int) -> np.ndarray:
    if payload_type not in CODECS:
        raise ValueError(f"unsupported payload type {payload_type}")
    return CODECS[payload_type][2](data)


def float_to_pcm16(x: np.ndarray) -> np.ndarray:
    """Engine float samples (-1..1) → int16, clipped rather than wrapped."""
    return (np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0) * 32767.0
            ).round().astype(np.int16)


def pcm16_to_float(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float32) / 32768.0
