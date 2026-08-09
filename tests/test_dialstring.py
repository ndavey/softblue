"""Dial-string parsing."""

from __future__ import annotations

import pytest

from softblue.dialstring import (
    LONG_PAUSE_S,
    SHORT_PAUSE_S,
    DialStringError,
    describe,
    duration_estimate,
    parse,
)


def kinds(text):
    return [(s.kind, s.value or s.seconds) for s in parse(text)]


def test_adjacent_digits_merge_into_one_burst():
    assert kinds("2125551337") == [("dtmf", "2125551337")]


def test_dashes_are_ignored_so_numbers_read_naturally():
    assert parse("212-555-1337")[0].value == "2125551337"
    assert parse("212 555 1337")[0].value == "2125551337"


def test_the_actual_redbox_flow():
    """Press 2 for the section, pause, then dial the number."""
    assert kinds("2;212-555-1337") == [
        ("dtmf", "2"), ("wait", LONG_PAUSE_S), ("dtmf", "2125551337")]


def test_pause_characters():
    assert kinds(",") == [("wait", SHORT_PAUSE_S)]
    assert kinds(";") == [("wait", LONG_PAUSE_S)]
    assert kinds("w2.5") == [("wait", 2.5)]
    assert kinds("w10") == [("wait", 10.0)]


def test_coin_groups():
    assert kinds("[q]") == [("coins", "q")]
    assert kinds("[qq]") == [("coins", "qq")]
    assert kinds("[nd$]") == [("coins", "nd$")]


def test_full_script_round_trip():
    steps = parse("2;2125551337;w5[q]w10")
    assert [s.kind for s in steps] == [
        "dtmf", "wait", "dtmf", "wait", "wait", "coins", "wait"]
    assert "DTMF 2125551337" in describe(steps)
    assert duration_estimate(steps) == pytest.approx(
        LONG_PAUSE_S * 2 + 5 + 10 + 0.2 + 10 * 0.2 + 0.25, abs=0.01)


def test_star_hash_and_letters_are_dtmf():
    assert kinds("*#ABCD") == [("dtmf", "*#ABCD")]
    assert parse("abcd")[0].value == "ABCD"      # normalised


@pytest.mark.parametrize("bad,msg", [
    ("[q", "unclosed"),
    ("1]", "unmatched"),
    ("[]", "empty"),
    ("[z]", "unknown coin"),
    ("w", "needs a duration"),
    ("wxyz", "needs a duration"),
    ("2%3", "unexpected"),
])
def test_errors_are_specific(bad, msg):
    with pytest.raises(DialStringError, match=msg):
        parse(bad)


def test_empty_string_is_no_steps():
    assert parse("") == [] and parse("   ") == []
