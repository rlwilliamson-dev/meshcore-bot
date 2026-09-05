#!/usr/bin/env python3
"""Unit tests for the shared weather-message formatters (modules/wx_format).

These lock the on-mesh look used by both wx (NOAA) and gwx (Open-Meteo): line
shapes, conditional omission (feels/UV/precip), condition cleanup, and — most
importantly — that no block can exceed the channel byte budget.
"""
from modules.wx_format import (
    clean_condition,
    current_block,
    day_row,
    dewpoint_f,
    format_wind,
    hourly_row,
    pack,
    rh_from_dewpoint_f,
    tomorrow_block,
)

CH = 145   # channel budget (tighter than the 158 DM budget)


def _b(s):
    return len(s.encode("utf-8"))


# --- clean_condition ---------------------------------------------------------

def test_clean_condition_rewrites():
    assert clean_condition("Showers And Thunderstorms Likely") == "t-storms likely"
    assert clean_condition("Chance Showers And Thunderstorms") == "chance t-storms"
    # "X then Y" keeps only the imminent condition X (no bloated "X/Y…").
    assert clean_condition("Slight Chance Showers And Thunderstorms then Showers And Thunderstorms") == "slight t-storms"
    assert clean_condition("Mostly Cloudy then Chance Showers And Thunderstorms") == "mostly cloudy"
    assert clean_condition("Slight Chance Showers And Thunderstorms") == "slight t-storms"
    assert clean_condition("Mostly Sunny") == "mostly sunny"
    assert clean_condition("") == ""
    assert clean_condition(None) == ""


def test_clean_condition_caps_length():
    out = clean_condition("a very long winded weather description that rambles", cap=16)
    assert len(out) <= 17 and out.endswith("…")


# --- format_wind -------------------------------------------------------------

def test_format_wind():
    assert format_wind("SSW", 10) == "SSW 10"
    assert format_wind("N", 2) == "calm"      # below threshold
    assert format_wind(None, 12) == "calm"    # no direction
    assert format_wind("WNW", "25") == "WNW 25"


# --- current_block -----------------------------------------------------------

def test_current_block_full():
    out = current_block("Nashville, TN", "🌦️", "Showers And Thunderstorms Likely", 41, 86,
                        feels=88, hi=88, lo=72, wind="SSW 10", rh=73, dew=69, uv=7)
    assert out == ("Nashville, TN 🌦️ t-storms likely 41%\n"
                   "now 86°F  feels 88°  H88° L72°\n"
                   "wind SSW 10 | RH 73% | dew 69° | UV 7")


def test_current_block_omits_when_not_meaningful():
    # feels==temp -> omit; uv<=2 -> omit; precip<15 -> omit
    out = current_block("Nashville, TN", "☀️", "Clear", 5, 68,
                        feels=68, hi=88, lo=66, wind="calm", rh=55, dew=51, uv=0)
    assert "feels" not in out
    assert "UV" not in out
    assert not out.split("\n")[0].endswith("%")  # no precip tail
    assert "now 68°F  H88° L66°" in out


def test_current_block_worst_case_fits_channel():
    out = current_block("Ouagadougou, Burkina Faso", "🌨️", "Heavy Freezing Drizzle", 100, 105,
                        feels=115, hi=108, lo=88, wind="WNW 25", rh=100, dew=78, uv=11, budget=CH)
    assert _b(out) <= CH


def test_current_block_ellipsizes_pathological_location():
    out = current_block("X" * 80, "🌨️", "Heavy Freezing Drizzle", 100, 105,
                        feels=115, hi=108, lo=88, wind="WNW 25", rh=100, dew=78, uv=11, budget=CH)
    assert _b(out) <= CH  # never overflows even with an absurd location


# --- tomorrow / rows ---------------------------------------------------------

def test_tomorrow_block():
    out = tomorrow_block("Nashville, TN", "Mon", "🌦️", "Showers And Thunderstorms",
                         85, 72, 60, "SSW 5", "Showers And Thunderstorms Likely")
    assert out.startswith("Nashville, TN · Tomorrow Mon\n")
    assert "🌦️ t-storms  H85° L72° 60%" in out
    assert "night: t-storms likely" in out


def test_day_row():
    assert day_row("Mon", "🌦️", "Showers And Thunderstorms", 85, 72) == "Mon 🌦️ t-storms  85°/72°"


def test_hourly_row_precip_floor():
    assert hourly_row("4PM", "🌦️", 83, 41) == "4PM 🌦️ 83° 41%"
    assert hourly_row("8PM", "⛅", 80, 10) == "8PM ⛅ 80°"   # <15% -> dropped


# --- pack --------------------------------------------------------------------

def test_pack_splits_and_respects_budget():
    rows = [day_row(d, "🌦️", "Showers And Thunderstorms", 90, 70) for d in
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    msgs = pack("Nashville, TN · 7-day", rows, budget=158)
    assert len(msgs) >= 2
    assert all(_b(m) <= 158 for m in msgs)
    # every row survives exactly once across the messages
    joined = "\n".join(msgs)
    for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert joined.count(f"{d} ") == 1


# --- dewpoint / RH derivation (fills NOAA obs gaps) ---------------------------

def test_dewpoint_f_known_values():
    assert dewpoint_f(85, 50) == 64       # NWS calculator ~64
    assert dewpoint_f(72, 100) == 72      # saturated -> dew == temp
    assert abs(dewpoint_f(95, 30) - 58) <= 1
    # dew point is always <= temperature
    assert dewpoint_f(90, 40) <= 90


def test_rh_from_dewpoint_f_known_values():
    assert rh_from_dewpoint_f(72, 72) == 100
    assert abs(rh_from_dewpoint_f(85, 64) - 50) <= 1
    assert 0 <= rh_from_dewpoint_f(95, 40) <= 100


def test_dewpoint_rh_roundtrip():
    for t, rh in ((80, 65), (60, 40), (95, 55), (50, 80)):
        assert abs(rh_from_dewpoint_f(t, dewpoint_f(t, rh)) - rh) <= 1


def test_dewpoint_guards():
    assert dewpoint_f(80, 0) is None
    assert dewpoint_f(None, 50) is None
    assert dewpoint_f(80, "x") is None
    assert rh_from_dewpoint_f("x", 60) is None
    assert rh_from_dewpoint_f(80, None) is None
