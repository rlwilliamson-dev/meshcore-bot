#!/usr/bin/env python3
"""Tests for gwx's hourly forecast (previously advertised but unimplemented) and
its retry-enabled Open-Meteo session.

format_hourly_forecast is exercised directly with synthetic Open-Meteo hourly
arrays (no network); the option-parsing wiring is checked by driving execute()
with the forecast fetch stubbed and asserting forecast_type='hourly' reaches it.
"""

import asyncio
import configparser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.i18n import Translator
from modules.models import MeshMessage

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSLATIONS = str(REPO_ROOT / "translations")


def _gwx():
    cfg = configparser.ConfigParser()
    for section in ("Bot", "Weather"):
        cfg.add_section(section)
    cfg.set("Bot", "bot_name", "WeatherBot-V3")
    cfg.set("Weather", "temperature_unit", "fahrenheit")
    bot = SimpleNamespace(
        logger=Mock(),
        config=cfg,
        db_manager=Mock(),
        translator=Translator(language="en", translation_path=TRANSLATIONS),
        command_manager=SimpleNamespace(),
    )
    return GlobalWxCommand(bot)


def _hourly_data():
    # 'current' is 2:30PM, so the first shown hour should be 3PM (15:00).
    return {
        "current": {"time": "2026-06-06T14:30"},
        "hourly": {
            "time": [
                "2026-06-06T14:00", "2026-06-06T15:00", "2026-06-06T16:00",
                "2026-06-06T17:00", "2026-06-06T18:00",
            ],
            "temperature_2m": [72, 73, 71, 68, 66],
            "weather_code": [0, 1, 3, 61, 80],
        },
    }


# --- formatter --------------------------------------------------------------

def test_hourly_starts_at_next_whole_hour():
    out = _gwx().format_hourly_forecast(_hourly_data(), max_length=158, location="Tokyo, Japan")
    assert out.startswith("Tokyo, Japan · hourly\n")  # header (style A rows below)
    assert "3PM" in out          # the upcoming hour
    assert "2PM" not in out      # the in-progress hour is skipped
    assert "73°" in out          # 3PM temperature


def test_hourly_respects_budget():
    c = _gwx()
    full = c.format_hourly_forecast(_hourly_data(), max_length=158, location="Tokyo, Japan")
    tight = c.format_hourly_forecast(_hourly_data(), max_length=40, location="Tokyo, Japan")
    assert tight.startswith("Tokyo, Japan · hourly")
    assert len(tight.encode("utf-8")) <= 40   # pack respects the byte budget
    assert len(tight) < len(full)             # fewer hours fit


def test_hourly_caps_at_twelve_hours():
    data = {
        "current": {"time": "2026-06-06T00:00"},
        "hourly": {
            "time": [f"2026-06-06T{h:02d}:00" for h in range(24)],
            "temperature_2m": list(range(24)),
            "weather_code": [0] * 24,
        },
    }
    out = _gwx().format_hourly_forecast(data, max_length=10_000, location="X")
    assert out.count("AM") + out.count("PM") <= 12  # capped at 12 rows


def test_hourly_empty_data():
    assert _gwx().format_hourly_forecast({"hourly": {}}, max_length=158) == "Hourly forecast not available"


# --- option parsing wiring --------------------------------------------------

def test_hourly_option_is_parsed_and_routed():
    cmd = _gwx()
    captured_type = {}

    def _fake_open_meteo(lat, lon, forecast_type="default", **kwargs):
        captured_type["ft"] = forecast_type
        return "Hourly: 3PM ☀️73°"

    cmd.geocode_location = Mock(return_value=(35.68, 139.65, {"country_code": "jp", "city": "Tokyo"}, None))
    cmd._format_location_display = Mock(return_value="Tokyo, Japan")
    cmd.get_open_meteo_weather = _fake_open_meteo

    captured: list[str] = []

    async def _send(message, content, **kwargs):
        captured.append(content)
        return True

    cmd.bot.command_manager.send_response = _send
    msg = MeshMessage(content="gwx tokyo hourly", channel=None, is_dm=True, sender_id="U1")
    assert asyncio.run(cmd.execute(msg)) is True
    assert captured_type.get("ft") == "hourly"     # 'hourly' was parsed, not left in the location
    assert captured and captured[0] == "Hourly: 3PM ☀️73°"   # location now embedded by the formatter, not prepended


# --- retry session (resilience parity with wx) ------------------------------

def test_gwx_has_retry_session():
    cmd = _gwx()
    assert isinstance(cmd.api_session, requests.Session)
    # The https adapter carries a non-zero retry policy.
    adapter = cmd.api_session.get_adapter("https://api.open-meteo.com")
    assert adapter.max_retries.total == 2
