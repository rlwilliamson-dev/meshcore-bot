#!/usr/bin/env python3
"""End-to-end tests for wx/gwx location handling — bare state/country + redirect.

Brings the wx (NOAA) and gwx (Open-Meteo) commands up to the same location-input
robustness as the rain/snow commands:

  * bare US state ("tennessee")  -> its capital ("Nashville, TN") + a heads-up note,
    instead of a meaningless state-centroid point;
  * bare foreign country ("france") or a foreign city ("tokyo", "paris, france")
    -> wx redirects to gwx (NOAA is US-only); gwx serves the capital + note;
  * a comma ("paris, tx") still means the user qualified a city, untouched.

The classifier (region_capital_lookup) is tested directly; the command behavior
is driven through execute() with the geocode + forecast-fetch seams stubbed, so
no network is touched. The reply is captured at command_manager.send_response.
"""

import asyncio
import configparser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand
from modules.i18n import Translator
from modules.location_format import region_suffix_from_address
from modules.models import MeshMessage
from modules.region_capitals import (
    REGION_DEFAULT_NOTE,
    region_capital_lookup,
    region_capital_query,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSLATIONS = str(REPO_ROOT / "translations")


# --- classifier (pure, no network) ------------------------------------------

def test_region_capital_lookup_us_state():
    assert region_capital_lookup("tennessee") == ("Nashville, TN", "state")
    assert region_capital_lookup("texas") == ("Austin, TX", "state")
    assert region_capital_lookup("  CALIFORNIA  ") == ("Sacramento, CA", "state")
    # US state wins over a same-named country.
    assert region_capital_lookup("georgia") == ("Atlanta, GA", "state")


def test_region_capital_lookup_country():
    assert region_capital_lookup("france") == ("Paris, France", "country")
    assert region_capital_lookup("uk") == ("London, United Kingdom", "country")
    assert region_capital_lookup("japan") == ("Tokyo, Japan", "country")


def test_region_capital_lookup_not_a_bare_region():
    # Cities, qualified "city, region", ZIPs, city-dominant states, empties.
    for val in ("nashville", "paris", "paris, france", "37013",
                "new york", "washington", "", "   "):
        assert region_capital_lookup(val) == (None, None), val
    assert region_capital_lookup(None) == (None, None)


def test_region_capital_query_back_compat():
    # The thin wrapper still returns just the query string.
    assert region_capital_query("france") == "Paris, France"
    assert region_capital_query("tennessee") == "Nashville, TN"
    assert region_capital_query("nashville") is None


# --- shared bot scaffolding -------------------------------------------------

def _make_bot(bot_name="WeatherBot-V3", weather_overrides=None):
    """Minimal bot: real ConfigParser + real Translator, capturing send_response.

    SimpleNamespace (not Mock) so get_max_message_length's hasattr(bot,'meshcore')
    is False and it falls back to the configured bot_name.
    """
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.set("Bot", "bot_name", bot_name)
    cfg.add_section("Channels")
    cfg.set("Channels", "monitor_channels", "general")
    cfg.set("Channels", "respond_to_dms", "true")
    cfg.add_section("Wx_Command")
    cfg.set("Wx_Command", "enabled", "true")
    cfg.add_section("Weather")
    cfg.set("Weather", "weather_provider", "noaa")  # wx does NOT delegate to gwx
    cfg.set("Weather", "default_state", "TN")
    cfg.set("Weather", "default_country", "US")
    cfg.set("Weather", "default_city", "")
    for key, val in (weather_overrides or {}).items():
        cfg.set("Weather", key, val)

    captured: list[str] = []

    async def _send_response(message, content, **kwargs):
        captured.append(content)
        return True

    async def _send_response_chunked(message, chunks, **kwargs):
        captured.extend(chunks)
        return True

    command_manager = SimpleNamespace(
        send_response=_send_response,
        send_response_chunked=_send_response_chunked,
        monitor_channels=["general"],
    )
    bot = SimpleNamespace(
        logger=Mock(),
        config=cfg,
        translator=Translator(language="en", translation_path=TRANSLATIONS),
        command_manager=command_manager,
        db_manager=Mock(),
    )
    return bot, captured


def _msg(content, is_dm=True):
    return MeshMessage(
        content=content,
        channel=None if is_dm else "general",
        is_dm=is_dm,
        sender_id="U1",
    )


# --- wx (NOAA): bare state, redirect ----------------------------------------

# Resolved-city geocoder fixtures. US entries carry the ISO3166-2 code that real
# Nominatim returns, so the state abbreviation resolves without the `us` library.
_WX_CITY_DB = {
    "nashville, tn": (36.1627, -86.7816, {"country_code": "us", "city": "Nashville", "state": "Tennessee", "ISO3166-2-lvl4": "US-TN"}),
    "nashville": (36.1627, -86.7816, {"country_code": "us", "city": "Nashville", "state": "Tennessee", "ISO3166-2-lvl4": "US-TN"}),
    "atlanta": (33.7490, -84.3880, {"country_code": "us", "city": "Atlanta", "state": "Georgia", "ISO3166-2-lvl4": "US-GA"}),
    "tokyo": (35.6762, 139.6503, {"country_code": "jp", "city": "Tokyo"}),
    "paris, france": (48.8566, 2.3522, {"country_code": "fr", "city": "Paris"}),
}


def _build_wx():
    bot, captured = _make_bot()
    cmd = WxCommand(bot)
    assert cmd.delegate_command is None, "wx must run the NOAA path, not delegate to gwx"
    cmd.city_to_lat_lon = Mock(side_effect=lambda loc: _WX_CITY_DB.get(loc.strip().lower(), (None, None, None)))
    cmd.get_noaa_weather = Mock(return_value=("Sunny 72°F", {"properties": {}}))
    cmd.get_weather_alerts_noaa = Mock(return_value=cmd.NO_ALERTS)
    return cmd, captured


def _run_wx(cmd, captured, content, *, is_dm=True):
    msg = _msg(content, is_dm=is_dm)
    assert asyncio.run(cmd.execute(msg)) is True
    assert len(captured) == 1, f"expected one reply, got {captured!r}"
    resp = captured[-1]
    assert len(resp.encode("utf-8")) <= cmd.get_max_message_length(msg)
    return resp


def test_wx_bare_state_resolves_to_capital_with_note():
    cmd, captured = _build_wx()
    resp = _run_wx(cmd, captured, "wx tennessee")
    # Geocoded the capital, not the bare state.
    cmd.city_to_lat_lon.assert_called_once_with("Nashville, TN")
    assert "Sunny 72°F" in resp
    assert resp.endswith(REGION_DEFAULT_NOTE)


def test_wx_bare_foreign_country_redirects_to_gwx_without_geocoding():
    cmd, captured = _build_wx()
    resp = _run_wx(cmd, captured, "wx france")
    assert resp == "🌍 france is outside NOAA's US coverage, try: gwx france"
    cmd.city_to_lat_lon.assert_not_called()  # short-circuits before any geocode
    cmd.get_noaa_weather.assert_not_called()


def test_wx_foreign_city_redirects_to_gwx():
    cmd, captured = _build_wx()
    resp = _run_wx(cmd, captured, "wx tokyo")
    assert resp == "🌍 tokyo is outside NOAA's US coverage, try: gwx tokyo"
    cmd.get_noaa_weather.assert_not_called()  # never reaches NOAA


def test_wx_qualified_foreign_city_redirects_and_is_not_treated_as_region():
    cmd, captured = _build_wx()
    resp = _run_wx(cmd, captured, "wx paris, france")
    # The comma means it's a city, so it geocodes (to FR) then redirects.
    assert resp == "🌍 paris, france is outside NOAA's US coverage, try: gwx paris, france"


def test_wx_normal_us_city_unchanged_no_note():
    cmd, captured = _build_wx()
    resp = _run_wx(cmd, captured, "wx nashville")
    assert "Sunny 72°F" in resp
    assert REGION_DEFAULT_NOTE not in resp
    cmd.get_noaa_weather.assert_called_once()


def test_wx_label_uses_abbreviation_via_iso():
    # The location label (state abbr from the ISO code, works without the `us`
    # lib) is passed to the formatter, which embeds it on line 1 / the header.
    cmd, captured = _build_wx()
    _run_wx(cmd, captured, "wx atlanta")
    assert cmd.get_noaa_weather.call_args.kwargs.get("location") == "Atlanta, GA"


# --- gwx (Open-Meteo): bare state/country served with note ------------------

def _build_gwx():
    bot, captured = _make_bot()
    cmd = GlobalWxCommand(bot)
    cmd.geocode_location = Mock(return_value=(48.8566, 2.3522, {"country_code": "fr"}, None))
    cmd._format_location_display = Mock(return_value="TestCity")
    cmd.get_open_meteo_weather = Mock(return_value="Clear 60°F")
    cmd._check_extreme_conditions = Mock(return_value=None)
    return cmd, captured


def _run_gwx(cmd, captured, content, *, is_dm=True):
    msg = _msg(content, is_dm=is_dm)
    assert asyncio.run(cmd.execute(msg)) is True
    assert len(captured) == 1, f"expected one reply, got {captured!r}"
    resp = captured[-1]
    assert len(resp.encode("utf-8")) <= cmd.get_max_message_length(msg)
    return resp


def test_gwx_bare_country_serves_capital_with_note():
    cmd, captured = _build_gwx()
    resp = _run_gwx(cmd, captured, "gwx france")
    # Resolved the capital query before geocoding (no redirect — gwx is global).
    assert cmd.geocode_location.call_args[0][0] == "Paris, France"
    assert "Clear 60°F" in resp
    assert resp.endswith(REGION_DEFAULT_NOTE)


def test_gwx_bare_us_state_serves_capital_with_note():
    cmd, captured = _build_gwx()
    resp = _run_gwx(cmd, captured, "gwx texas")
    assert cmd.geocode_location.call_args[0][0] == "Austin, TX"
    assert resp.endswith(REGION_DEFAULT_NOTE)


def test_gwx_normal_city_unchanged_no_note():
    cmd, captured = _build_gwx()
    resp = _run_gwx(cmd, captured, "gwx tokyo")
    assert cmd.geocode_location.call_args[0][0] == "tokyo"  # passed through as typed
    assert REGION_DEFAULT_NOTE not in resp


# --- shared region suffix (works without the optional `us` library) ---------

def test_region_suffix_prefers_iso_code():
    # Nominatim's ISO3166-2 subdivision code yields the state abbr even when the
    # `us` library isn't installed (the production case).
    assert region_suffix_from_address(
        {"country_code": "us", "ISO3166-2-lvl4": "US-TN", "state": "Tennessee"}
    ) == "TN"


def test_region_suffix_falls_back_to_state_name():
    # No ISO code: 'TN' if the `us` lib is present, else the raw state name.
    assert region_suffix_from_address({"country_code": "us", "state": "Tennessee"}) in ("TN", "Tennessee")


def test_region_suffix_international_is_country_name():
    assert region_suffix_from_address({"country_code": "fr", "country": "France"}) == "France"
    assert region_suffix_from_address({}) is None


# --- gwx label formatter (cohesion: full country names, ISO state, dedup) ----

def _plain_gwx():
    bot, _ = _make_bot()
    return GlobalWxCommand(bot)


def test_gwx_label_us_uses_state_abbreviation():
    label = _plain_gwx()._format_location_display(
        {"country_code": "us", "city": "Nashville", "state": "Tennessee", "ISO3166-2-lvl4": "US-TN"},
        None, "nashville",
    )
    assert label == "Nashville, TN"


def test_gwx_label_international_uses_full_country_name():
    # Cohesion win: 'Paris, France', not the old 'Paris, FR' country code.
    label = _plain_gwx()._format_location_display(
        {"country_code": "fr", "city": "Paris", "country": "France"}, None, "paris, france",
    )
    assert label == "Paris, France"


def test_gwx_label_dedupes_city_state():
    label = _plain_gwx()._format_location_display(
        {"country_code": "sg", "city": "Singapore", "country": "Singapore"}, None, "singapore",
    )
    assert label == "Singapore"


# --- "help" as an argument shows usage, not a geocode of the word "help" -----

def test_wx_help_arg_shows_usage_not_geocode():
    cmd, captured = _build_wx()
    _run_wx(cmd, captured, "wx help")
    assert captured[0].startswith("wx <")          # the usage string
    cmd.city_to_lat_lon.assert_not_called()         # did NOT geocode "help"


def test_gwx_help_arg_shows_usage_not_geocode():
    cmd, captured = _build_gwx()
    _run_gwx(cmd, captured, "gwx help")
    assert captured[0].startswith("gwx <")
    cmd.geocode_location.assert_not_called()


def test_coords_label_abbreviates_state_via_iso():
    # ZIP/coordinate labels must abbreviate the state via the ISO3166-2 code,
    # not the full name (the `us` lib isn't installed in prod). Regression for
    # "Hendersonville, Tennessee" -> "Hendersonville, TN".
    from unittest.mock import patch
    bot, _ = _make_bot()
    cmd = WxCommand(bot)
    fake = Mock()
    fake.raw = {"address": {"city": "Hendersonville", "state": "Tennessee",
                            "country_code": "us", "ISO3166-2-lvl4": "US-TN"}}
    with patch("modules.utils.rate_limited_nominatim_reverse_sync", return_value=fake):
        assert cmd._coordinates_to_location_string(36.30, -86.62) == "Hendersonville, TN"


def test_zip_to_city_string_prefers_usps_city_over_county():
    # Rural ZIPs reverse-geocode to a county; Zippopotam gives the USPS city.
    # Regression for 40965 -> "Bell County" instead of "Middlesboro".
    from unittest.mock import patch

    import modules.location_format as lf
    lf._ZIP_CITY_CACHE.pop("40965", None)
    fake = Mock()
    fake.ok = True
    fake.json = lambda: {"places": [{"place name": "Middlesboro", "state abbreviation": "KY"}]}
    with patch("requests.get", return_value=fake):
        assert lf.zip_to_city_string("40965") == "Middlesboro, KY"


# --- wx: a ZIP is a complete location, trailing junk is a bad option ---------

def test_wx_zip_with_mistyped_option_reports_unknown_option():
    """Regression: "wx 37138 houry" (mistyped "hourly") geocoded the whole string
    as a city name, fuzzy-matched somewhere outside the US, and told the user the
    ZIP was outside NOAA's coverage. Report the bad option instead.
    """
    cmd, captured = _build_wx()
    resp = _run_wx(cmd, captured, "wx 37138 houry")
    assert "houry" in resp
    assert "outside NOAA" not in resp
    cmd.city_to_lat_lon.assert_not_called()


def test_wx_multiword_city_is_not_mistaken_for_a_bad_option():
    """The guard keys off a leading 5-digit ZIP, so ordinary multi-word locations
    (and "city, state") still geocode normally."""
    cmd, captured = _build_wx()
    _run_wx(cmd, captured, "wx nashville, tn")
    cmd.city_to_lat_lon.assert_called()
