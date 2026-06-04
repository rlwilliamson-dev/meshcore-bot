#!/usr/bin/env python3
"""LOCAL-ONLY tests (not upstream) for the BNA-WX-BOT customizations.

Covers the URL-safe alert chunker. See /Users/ryan/wx-bot-local-mods.md.
"""

from modules.service_plugins.weather_service import ALERT_CHUNK_BYTES, chunk_alert_text


def test_short_alert_one_message_url_intact():
    out = chunk_alert_text("🟡 Wind Advisory for King County until 9 PM.", "https://is.gd/abc")
    assert len(out) == 1
    assert out[0].endswith("https://is.gd/abc")


def test_long_alert_chunks_url_whole_at_end():
    body = "🟠 Severe Thunderstorm Warning for Davidson County until 8:45 PM. " + (
        "60 mph gusts and quarter-size hail likely. Move to an interior room. " * 4
    )
    out = chunk_alert_text(body, "https://is.gd/xyz789")
    assert len(out) > 1
    # URL appears whole in exactly one message
    assert sum("https://is.gd/xyz789" in m for m in out) == 1
    # no message exceeds the cap (+3 for the "..." continuation prefix)
    assert all(len(m.encode()) <= ALERT_CHUNK_BYTES + 3 for m in out)
    # continuation chunks are prefixed "..."
    assert all(m.startswith("...") for m in out[1:])
    assert not out[0].startswith("...")


def test_oversized_url_gets_its_own_message():
    huge = "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0." + ("x" * 160)
    out = chunk_alert_text("🟠 Alert body here that is reasonably sized.", huge)
    # the link is never split: it's whole in exactly one message
    assert sum(huge in m for m in out) == 1


def test_no_url():
    out = chunk_alert_text("🟡 Short alert, no link.")
    assert out == ["🟡 Short alert, no link."]


def test_empty_body_with_url():
    out = chunk_alert_text("", "https://is.gd/x")
    assert any("https://is.gd/x" in m for m in out)


def test_words_never_split():
    # every whitespace-token in the input survives intact across the chunks
    body = " ".join(f"word{i}" for i in range(60))
    out = chunk_alert_text(body)
    rejoined = " ".join(m.removeprefix("...") for m in out)
    for i in range(60):
        assert f"word{i}" in rejoined.split()
