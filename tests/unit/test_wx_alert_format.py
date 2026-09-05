#!/usr/bin/env python3
"""Unit tests for the shared NWS-alert formatters (modules/wx_format).

These lock the proactive alert-push look and its invariants:
  * the event name is the canonical cap:event (no dropped "Watch"/"Warning"),
  * pure-boilerplate NWS headlines are dropped, real action text is kept,
  * long marine zone names + headlines never blow the byte budget, and
  * THE LINK IS ALWAYS WHOLE — appended to the last message or its own, never cut.
"""
from modules.wx_format import (
    alert_emoji,
    alert_lines,
    alert_location,
    clean_headline,
    deshout,
    headline_is_boilerplate,
    pack_alert,
)

CH = 144  # the service's channel-body budget (ALERT_CHUNK_BYTES)


def _b(s):
    return len(s.encode("utf-8"))


# --- emoji / location --------------------------------------------------------

def test_alert_emoji():
    assert alert_emoji("Extreme") == "🔴"
    assert alert_emoji("Severe") == "🟠"
    assert alert_emoji("Moderate") == "🟡"
    assert alert_emoji("Minor") == "⚪"
    assert alert_emoji("Unknown") == "⚪"
    assert alert_emoji(None) == "⚪"
    assert alert_emoji("bogus") == "⚪"


def test_alert_location_first_plus_more():
    assert alert_location("Davidson") == "Davidson"
    assert alert_location("Davidson;Wilson;Smith") == "Davidson +2 more"
    assert alert_location("") == ""
    assert alert_location(None) == ""
    # a 100+ char marine zone is ellipsized so it can't blow the line
    zone = "Waters from Pt. Sal to Santa Cruz Island CA and westward 60 nm including the islands"
    out = alert_location(zone)
    assert out.endswith("…") and len(out) <= 45


_DAV_NBRS = {"Cheatham", "Robertson", "Rutherford", "Sumner", "Williamson", "Wilson"}


def test_alert_location_leads_with_home_county():
    area = "Cheatham, TN; Davidson, TN; Robertson, TN"
    assert alert_location(area) == "Cheatham, TN +2 more"               # no context: first area
    assert alert_location(area, home="Davidson") == "Davidson +2 more"  # home leads, no neighbors
    assert alert_location(area, home="Wilson") == "Cheatham, TN +2 more"  # home absent -> legacy


def test_alert_location_names_adjacent_counties():
    area = "Cheatham, TN; Davidson, TN; Robertson, TN"
    # all three are home + adjacent -> all named, home first
    assert alert_location(area, home="Davidson", neighbors=_DAV_NBRS) == "Davidson, Cheatham, Robertson"
    # sub-county areas dedupe to their county
    sub = "Northwestern Davidson, TN; Eastern Davidson, TN; Western Cheatham, TN"
    assert alert_location(sub, home="Davidson", neighbors=_DAV_NBRS) == "Davidson, Cheatham"
    # big alert: home + 2 adjacent + many far -> name local (capped at 4), collapse rest
    big = "; ".join(["Davidson, TN", "Cheatham, TN", "Williamson, TN"] + [f"Far{i}, KY" for i in range(35)])
    out = alert_location(big, home="Davidson", neighbors=_DAV_NBRS, max_names=4)
    assert out == "Davidson, Cheatham, Williamson +35 more"
    # home present but only far counties otherwise
    assert alert_location("Davidson, TN; Maury, TN; Giles, TN", home="Davidson",
                          neighbors=_DAV_NBRS) == "Davidson +2 more"


def test_alert_lines_tornado_lists_all_adjacent():
    area = "; ".join(f"{c}, TN" for c in
                     ["Davidson", "Cheatham", "Robertson", "Rutherford", "Sumner", "Williamson", "Wilson"])
    # tornado: the 4-county cap is lifted -> every adjacent county is named
    tor = alert_lines("Extreme", "Tornado Warning", area, "Jun 9 7PM", "NWS Nashville TN", "",
                      home="Davidson", neighbors=_DAV_NBRS)
    assert tor[0] == "🔴 Tornado Warning · Davidson, Cheatham, Robertson, Rutherford, Sumner, Williamson, Wilson"
    # non-tornado, same area -> still capped at 4
    sev = alert_lines("Severe", "Severe Thunderstorm Warning", area, "Jun 9 7PM", "NWS Nashville TN", "",
                      home="Davidson", neighbors=_DAV_NBRS)
    assert sev[0].endswith("+3 more")
    # tornado still collapses NON-adjacent counties
    mixed = "Davidson, TN; Cheatham, TN; " + "; ".join(f"Far{i}, KY" for i in range(5))
    tor2 = alert_lines("Severe", "Tornado Watch", mixed, "Jun 9 7PM", "NWS Nashville TN", "",
                       home="Davidson", neighbors=_DAV_NBRS)
    assert tor2[0] == "🟠 Tornado Watch · Davidson, Cheatham +5 more"


# --- boilerplate detection (the hybrid keep/drop rule) -----------------------

def test_headline_boilerplate_dropped():
    assert headline_is_boilerplate("FLOOD WATCH IN EFFECT THROUGH MONDAY AFTERNOON", "Flood Watch")
    assert headline_is_boilerplate(
        "SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT UNTIL 11 PM CDT FOR BURLEIGH EMMONS GRANT",
        "Severe Thunderstorm Warning",
    )
    assert headline_is_boilerplate("GALE WARNING REMAINS IN EFFECT UNTIL 3 AM PDT MONDAY", "Gale Watch")
    assert headline_is_boilerplate("", "Flood Watch")
    assert headline_is_boilerplate(None, "Flood Watch")


def test_headline_action_text_kept():
    assert not headline_is_boilerplate(
        "TAKE COVER NOW. A CONFIRMED TORNADO WAS LOCATED NEAR ROGERSVILLE MOVING NORTHEAST AT 45 MPH",
        "Tornado Warning",
    )
    assert not headline_is_boilerplate(
        "FLASH FLOOD EMERGENCY FOR NASHVILLE. MOVE TO HIGHER GROUND NOW. LIFE-THREATENING SITUATION",
        "Flash Flood Warning",
    )
    assert not headline_is_boilerplate(
        "STRONG STORMS WILL IMPACT PORTIONS OF MIDDLE TENNESSEE WITH DAMAGING WINDS", "Special Weather Statement"
    )


def test_deshout():
    assert deshout("FLOOD WATCH IN EFFECT") == "Flood Watch In Effect"
    assert deshout("Already Mixed Case") == "Already Mixed Case"   # left alone
    assert deshout("") == ""
    assert deshout(None) == ""


# --- alert_lines: canonical event, boilerplate drop, action keep -------------

def test_alert_lines_core_two_lines():
    lines = alert_lines("Severe", "Flood Watch", "Adams;Boone;Cole", "Jun 8 1PM",
                        "NWS St Louis MO", "FLOOD WATCH IN EFFECT THROUGH MONDAY")
    assert lines == ["🟠 Flood Watch · Adams +2 more", "until Jun 8 1PM · NWS St Louis MO"]


def test_alert_lines_keeps_action_headline():
    lines = alert_lines("Extreme", "Tornado Warning", "Wright;Douglas", "Jun 7 10PM",
                        "NWS Nashville TN",
                        "TAKE COVER NOW. A CONFIRMED TORNADO MOVING NORTHEAST AT 45 MPH")
    assert lines[0] == "🔴 Tornado Warning · Wright +1 more"
    assert len(lines) == 3
    assert lines[2].startswith("Take Cover Now")   # de-shouted, kept


def test_alert_lines_missing_fields():
    # no expires, no office, no headline -> just the event/area line
    assert alert_lines("Minor", "Air Quality Alert", "Davidson", "", "", "") == [
        "⚪ Air Quality Alert · Davidson"
    ]


def test_alert_lines_collapses_multiline_headline():
    # A headline with newlines (e.g. a raw NWS product accidentally passed through)
    # must never leave an embedded newline — it'd chunk into garbage on the mesh.
    lines = alert_lines("Severe", "Tornado Warning", "Wright", "Jun 7 10PM", "NWS Nashville TN",
                        "First.\n\nTake cover now and move to a safe place immediately.")
    assert len(lines) == 3 and "\n" not in lines[2]


# --- pack_alert: budget + THE LINK INVARIANT ---------------------------------

def _url_whole_once(msgs, url):
    """url must appear intact (as a substring) in exactly one chunk."""
    return sum(url in m for m in msgs) == 1


def test_pack_alert_link_appended_when_fits():
    lines = ["🟠 Flood Watch · Adams +2 more", "until Jun 8 1PM · NWS St Louis MO"]
    url = "https://is.gd/abc"
    msgs = pack_alert(lines, url, budget=CH)
    assert len(msgs) == 1
    assert msgs[0].endswith(url)
    assert _url_whole_once(msgs, url)


def test_pack_alert_link_own_message_when_tight():
    # a near-budget single line forces the link onto its own final message, still whole
    line = "🟠 " + "x" * 130
    url = "https://is.gd/abcdef"
    msgs = pack_alert([line], url, budget=CH)
    assert _url_whole_once(msgs, url)
    assert msgs[-1] == url                       # link alone, intact
    assert all(_b(m) <= CH for m in msgs[:-1])


def test_pack_alert_link_never_split_even_if_oversize():
    # an un-shortenable giant link still goes out whole on its own line
    url = "https://example.com/" + "z" * 200
    msgs = pack_alert(["🟠 Flood Watch · Davidson"], url, budget=CH)
    assert _url_whole_once(msgs, url)
    assert url in msgs[-1]


def test_pack_alert_long_headline_chunks_and_keeps_link_whole():
    long_action = ("Take cover now. A confirmed large and destructive tornado was located near "
                   "Rogersville moving northeast at 45 mph. This is a life-threatening situation "
                   "for the warned area; seek shelter immediately on the lowest floor.")
    lines = ["🔴 Tornado Warning · Wright +2 more", "until Jun 7 10PM · NWS Nashville TN", long_action]
    url = "https://is.gd/tor9"
    msgs = pack_alert(lines, url, budget=CH)
    assert len(msgs) >= 2
    assert all(_b(m) <= CH for m in msgs)        # every chunk within budget
    assert _url_whole_once(msgs, url)            # link intact in exactly one chunk
    # continuation chunks are marked
    assert all(m.startswith("…") for m in msgs[1:])


def test_pack_alert_empty():
    assert pack_alert([], "") == [""]


# --- clean_headline: NWS '...' delimiters + worst-case shapes ----------------

# The actual Special Weather Statement that rendered as garbled "...Davidson...
# Marshall..." on the mesh (2026-06-18) — the regression this fix targets.
_SPS = ("...STRONG THUNDERSTORMS WILL IMPACT SOUTHWESTERN DAVIDSON...NORTH CENTRAL "
        "MARSHALL...NORTHERN MAURY AND WILLIAMSON COUNTIES...")


def test_clean_headline_sps_dots_become_commas():
    out = clean_headline(_SPS)
    assert "..." not in out and "…" not in out
    assert not out.startswith(",") and not out.endswith(",")
    assert out == ("Strong Thunderstorms Will Impact Southwestern Davidson, "
                   "North Central Marshall, Northern Maury And Williamson Counties")


def test_clean_headline_strips_leading_trailing_dots():
    assert clean_headline("...A...B...") == "A, B"
    assert clean_headline("…A…B…") == "A, B"            # unicode-ellipsis variant


def test_clean_headline_handles_spaces_and_runs():
    assert clean_headline("A ... B ...C") == "A, B, C"  # dots padded with spaces
    assert clean_headline("A....B..C") == "A, B, C"     # 4- and 2-dot runs
    assert clean_headline("DAVIDSON,...MAURY") == "Davidson, Maury"  # comma+dots -> one comma


def test_clean_headline_keeps_real_sentences():
    # single periods are sentence punctuation, NOT delimiters -> left intact
    assert clean_headline("Take cover now. Move to shelter.") == "Take cover now. Move to shelter."
    assert clean_headline("Winds to 60 mph. Penny size hail.") == "Winds to 60 mph. Penny size hail."


def test_clean_headline_collapses_newlines():
    assert clean_headline("First...Second\n\nthird line") == "First, Second third line"


def test_clean_headline_degenerate_inputs():
    assert clean_headline(None) == ""
    assert clean_headline("") == ""
    assert clean_headline("......") == ""              # dots only -> nothing
    assert clean_headline("  ...  ") == ""             # dots + whitespace only
    assert clean_headline(", , ,") == ""               # commas only


def test_clean_headline_already_clean_unchanged():
    assert clean_headline("Davidson, Maury and Williamson") == "Davidson, Maury and Williamson"


# --- alert_lines: the SPS renders clean + degenerate headline dropped --------

def test_alert_lines_sps_headline_is_clean():
    lines = alert_lines("Moderate", "Special Weather Statement",
                        "Davidson, TN; Williamson, TN; Marshall, TN; Maury, TN",
                        "Jun 18 12PM", "NWS Nashville TN", _SPS,
                        home="Davidson", neighbors=_DAV_NBRS)
    assert lines[0] == "🟡 Special Weather Statement · Davidson, Williamson +2 more"
    assert lines[1] == "until Jun 18 12PM · NWS Nashville TN"
    assert "..." not in lines[2] and lines[2].startswith("Strong Thunderstorms Will Impact")


def test_alert_lines_drops_dots_only_headline():
    # a headline that carries no real text must NOT add a garbage 3rd line
    lines = alert_lines("Minor", "Special Weather Statement", "Davidson", "Jun 18 12PM",
                        "NWS Nashville TN", "......")
    assert len(lines) == 2


def test_alert_lines_long_dotted_headline_capped_clean():
    # worst case: a very long dotted area list -> capped, no '...', no trailing comma,
    # ends with the continuation ellipsis, and chunks within budget with the link whole.
    long_sps = ("...STRONG STORMS WILL IMPACT "
                + "...".join(f"{c} COUNTY" for c in
                             ["DAVIDSON", "WILLIAMSON", "MARSHALL", "MAURY", "RUTHERFORD",
                              "WILSON", "SUMNER", "CHEATHAM", "ROBERTSON", "BEDFORD",
                              "COFFEE", "GILES"])
                + "...")
    lines = alert_lines("Moderate", "Special Weather Statement", "Davidson", "Jun 18 12PM",
                        "NWS Nashville TN", long_sps)
    assert "..." not in lines[2]
    assert lines[2].endswith("…") and not lines[2].rstrip("…").endswith(",")
    msgs = pack_alert(lines, "https://is.gd/sps1", budget=CH)
    assert all(_b(m) <= CH for m in msgs)
    assert _url_whole_once(msgs, "https://is.gd/sps1")


def test_pack_alert_sps_end_to_end_no_dots():
    lines = alert_lines("Moderate", "Special Weather Statement",
                        "Davidson, TN; Williamson, TN; Marshall, TN; Maury, TN",
                        "Jun 18 12PM", "NWS Nashville TN", _SPS,
                        home="Davidson", neighbors=_DAV_NBRS)
    msgs = pack_alert(lines, "https://v.gd/0tFVCH", budget=CH)
    assert all(_b(m) <= CH for m in msgs)
    assert all("..." not in m for m in msgs)          # no garbled dots anywhere
    assert _url_whole_once(msgs, "https://v.gd/0tFVCH")
    assert all(m == "https://v.gd/0tFVCH" or m.startswith("…") for m in msgs[1:])
