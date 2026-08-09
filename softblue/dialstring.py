"""Dial strings — a compact script for driving one call.

A whole IVR interaction is an ordered sequence of "send digits", "wait", "send
tones", so it reads better as one string than as a pile of flags:

    2;2125551337;w5[q]w10

Grammar (whitespace is ignored everywhere):

===============  ==========================================================
``0-9 * # A-D``  one DTMF digit, sent as an RFC 4733 event
``,``            pause 0.5 s
``;``            pause 2 s
``w<seconds>``   pause for an explicit time, e.g. ``w2`` or ``w1.5``
``[coins]``      PhreakMe coin tones as audio, e.g. ``[q]``, ``[qq]``, ``[nd]``
``-``            ignored, so ``212-555-1337`` can be written as dialled
===============  ==========================================================

Coin symbols inside ``[...]`` are the ``phreakme_coin`` ones: ``n`` nickel,
``d`` dime, ``q`` quarter, ``$`` dollar, ``c`` collect, ``r`` return.

There is no explicit "listen" step because the far end is recorded for the
whole call — a wait *is* a listen.
"""

from __future__ import annotations

from dataclasses import dataclass

SHORT_PAUSE_S = 0.5
LONG_PAUSE_S = 2.0

DTMF_CHARS = set("0123456789*#ABCD")
COIN_CHARS = set("ndq$cr")


class DialStringError(ValueError):
    pass


@dataclass
class Step:
    """One action. ``kind`` is 'dtmf', 'wait' or 'coins'."""

    kind: str
    value: str = ""
    seconds: float = 0.0

    def describe(self) -> str:
        if self.kind == "dtmf":
            return f"DTMF {self.value}"
        if self.kind == "wait":
            return f"wait {self.seconds:g}s"
        return f"coins {self.value}"


def parse(text: str) -> list[Step]:
    """Parse a dial string into steps, merging runs of adjacent DTMF digits."""
    steps: list[Step] = []
    i = 0
    text = text or ""

    def push_dtmf(ch: str) -> None:
        # Consecutive digits become one step so they are sent as a burst.
        if steps and steps[-1].kind == "dtmf":
            steps[-1].value += ch
        else:
            steps.append(Step("dtmf", ch))

    while i < len(text):
        ch = text[i]
        if ch.isspace() or ch == "-":
            i += 1
        elif ch.upper() in DTMF_CHARS:
            push_dtmf(ch.upper())
            i += 1
        elif ch == ",":
            steps.append(Step("wait", seconds=SHORT_PAUSE_S))
            i += 1
        elif ch == ";":
            steps.append(Step("wait", seconds=LONG_PAUSE_S))
            i += 1
        elif ch in "wW":
            j = i + 1
            while j < len(text) and (text[j].isdigit() or text[j] == "."):
                j += 1
            raw = text[i + 1:j]
            if not raw:
                raise DialStringError(
                    f"'w' at position {i} needs a duration, e.g. w2 or w1.5")
            try:
                secs = float(raw)
            except ValueError:
                raise DialStringError(f"bad wait duration {raw!r} at position {i}")
            if secs < 0:
                raise DialStringError(f"wait duration cannot be negative: {raw}")
            steps.append(Step("wait", seconds=secs))
            i = j
        elif ch == "[":
            end = text.find("]", i)
            if end == -1:
                raise DialStringError(f"unclosed '[' at position {i}")
            coins = "".join(text[i + 1:end].split())
            if not coins:
                raise DialStringError(f"empty coin group at position {i}")
            bad = [c for c in coins if c not in COIN_CHARS]
            if bad:
                raise DialStringError(
                    f"unknown coin symbol {bad[0]!r} — valid: "
                    f"{' '.join(sorted(COIN_CHARS))}")
            steps.append(Step("coins", coins))
            i = end + 1
        elif ch == "]":
            raise DialStringError(f"unmatched ']' at position {i}")
        else:
            raise DialStringError(
                f"unexpected {ch!r} at position {i}. Valid: digits * # A-D, "
                f"',' short pause, ';' long pause, 'w<sec>', '[coins]'.")
    return steps


def describe(steps: list[Step]) -> str:
    return " -> ".join(s.describe() for s in steps)


def duration_estimate(steps: list[Step], dtmf_each_s: float = 0.2) -> float:
    """Rough wall-clock estimate, for warning about absurdly long scripts."""
    total = 0.0
    for s in steps:
        if s.kind == "wait":
            total += s.seconds
        elif s.kind == "dtmf":
            total += len(s.value) * dtmf_each_s
        else:
            total += len(s.value) * 0.25
    return total
