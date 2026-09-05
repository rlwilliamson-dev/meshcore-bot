#!/usr/bin/env python3
"""Unit tests for the proactive NWS-alert poll's selection logic
(modules.service_plugins.weather_service.WeatherService._check_weather_alerts).

These lock the two fixes for "an active alert never reached the mesh":
  * FIRST poll after (re)start back-announces everything currently active,
    regardless of issue age — an alert already in effect at startup (e.g. an
    ongoing Flash Flood Warning) must NOT be silently swallowed as "old".
  * the poll queries BOTH the county zone (TNC, storm-based warnings) and the
    public forecast zone (TNZ, zone-based Watches/Advisories).
Later polls still only pick up alerts issued since the previous check, and
already-seen alert ids are never re-sent.
"""
import asyncio
import configparser
import datetime as dt
import time
from unittest.mock import Mock

from modules.service_plugins.weather_service import WeatherService


def _atom(entries):
    """entries: list of (alert_id, minutes_ago). Minimal ATOM the poll loop needs
    (id + updated); _parse_alert_entry is stubbed, so no further structure required."""
    now = dt.datetime.now(dt.timezone.utc)
    items = "".join(
        "<entry><id>{aid}</id><updated>{ts}</updated></entry>".format(
            aid=aid,
            ts=(now - dt.timedelta(minutes=ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for aid, ago in entries
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">' + items + "</feed>")


def build_alert_service(monkeypatch, *, atom, last_check, seen=None):
    """A WeatherService wired for the alert poll: county+forecast zones pinned via
    config (no network), api_session returns `atom`, format/send stubbed to isolate
    the selection logic. Returns (service, sent, urls)."""
    cfg = configparser.ConfigParser()
    cfg.add_section("Weather")
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "36.16")
    cfg.set("Weather_Service", "my_position_lon", "-86.78")
    cfg.set("Weather_Service", "alerts_channel", "bna-wx")
    cfg.set("Weather_Service", "alert_zone", "TNC037")          # county -> no /points call
    cfg.set("Weather_Service", "alert_forecast_zone", "TNZ027")  # forecast zone

    bot = Mock()
    bot.logger = Mock()
    bot.config = cfg
    bot.db_manager = Mock()

    service = WeatherService(bot)

    urls: list[str] = []

    def _get(url, **kwargs):
        urls.append(url)
        resp = Mock()
        resp.ok = True
        resp.status_code = 200
        resp.text = atom
        return resp

    service.api_session = Mock()
    service.api_session.get = _get

    # Isolate the time-window / zone selection from ATOM parsing + formatting + send.
    service._parse_alert_entry = lambda entry, aid: {
        "title": "Flash Flood Warning", "event": "Flash Flood Warning", "severity": "Severe",
    }

    async def _fmt(alert):
        return ["FFW"]

    sent: list[tuple] = []

    async def _send(channels, text):
        sent.append((tuple(channels), text))

    service._format_alert_full = _fmt
    service._send_to_channels = _send

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("modules.service_plugins.weather_service.asyncio.sleep", _noop)

    service.last_alert_check_time = last_check
    service.seen_alert_ids = set(seen or ())
    return service, sent, urls


# --- first poll: back-announce everything active --------------------------------

def test_first_poll_backfills_old_active_alert(monkeypatch):
    # An alert issued 64 min ago, first poll (last_check is None): must be SENT,
    # not swallowed as "older than one poll interval".
    service, sent, urls = build_alert_service(
        monkeypatch, atom=_atom([("FFW-1", 64)]), last_check=None)
    asyncio.run(service._check_weather_alerts())
    assert len(sent) == 1
    assert sent[0][0] == ("bna-wx",)
    assert "FFW-1" in service.seen_alert_ids
    assert service.last_alert_check_time is not None  # window armed for next poll


def test_first_poll_queries_both_county_and_forecast_zones(monkeypatch):
    service, sent, urls = build_alert_service(
        monkeypatch, atom=_atom([("FFW-1", 64)]), last_check=None)
    asyncio.run(service._check_weather_alerts())
    assert urls and "zone=TNC037,TNZ027" in urls[0]


# --- subsequent polls: only since last check, never re-send seen ----------------

def test_subsequent_poll_skips_alert_older_than_last_check(monkeypatch):
    # Not the first poll: a 64-min-old alert predates last_check -> NOT sent, but
    # marked seen so it won't churn.
    service, sent, urls = build_alert_service(
        monkeypatch, atom=_atom([("OLD-1", 64)]), last_check=time.time())
    asyncio.run(service._check_weather_alerts())
    assert sent == []
    assert "OLD-1" in service.seen_alert_ids


def test_subsequent_poll_sends_freshly_issued_alert(monkeypatch):
    # Issued 1 min ago, last check 10 min ago -> within window -> sent.
    service, sent, urls = build_alert_service(
        monkeypatch, atom=_atom([("NEW-1", 1)]), last_check=time.time() - 600)
    asyncio.run(service._check_weather_alerts())
    assert len(sent) == 1


def test_already_seen_alert_not_resent_even_on_first_poll(monkeypatch):
    service, sent, urls = build_alert_service(
        monkeypatch, atom=_atom([("DUP-1", 64)]), last_check=None, seen={"DUP-1"})
    asyncio.run(service._check_weather_alerts())
    assert sent == []
