"""Shared weather-message formatters for the wx (NOAA) and gwx (Open-Meteo) commands.

One place defines the on-mesh look so both commands render identically:

  current:   Nashville, TN 🌦️ t-storms likely 41%
             now 86°F  feels 88°  H88° L72°
             wind SSW 10 | RH 73% | dew 69° | UV 7

  tomorrow:  Nashville, TN · Tomorrow Mon
             🌦️ t-storms  H85° L72°  60%
             wind SSW 5 | night: t-storms likely

  7-day:     Nashville, TN · 7-day        hourly:  Nashville, TN · hourly
             Mon 🌦️ t-storms  85°/72°              4PM 🌦️ 83° 41%
             …                                      …

Each block is assembled against the message byte budget; the condition text is the
flex element and is ellipsized only if a pathological location would overflow.
Callers (wx/gwx) normalize their provider data into these args; the line shapes
live here so they can't drift. Pure functions, fully unit-testable, no I/O.
"""
import math
import re
from typing import Optional

# Ordered (lowercased) condition rewrites — applied longest/most-specific first.
_COND_REWRITES = [
    ("showers and thunderstorms", "t-storms"),
    ("thunderstorms", "t-storms"),
    ("thunderstorm", "t-storm"),
    ("rain showers", "showers"),
    ("snow showers", "snow"),
    ("slight chance", "slight"),
    ("isolated", "iso"),
    ("scattered", "sct"),
    ("areas of ", "areas "),
    (" and ", "/"),
]


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _ellipsize(s: str, max_bytes: int) -> str:
    """Trim s so it fits max_bytes (UTF-8), adding '…' when truncated."""
    if _bytes(s) <= max_bytes:
        return s
    if max_bytes <= 1:
        return ""
    out = s
    while out and _bytes(out + "…") > max_bytes:
        out = out[:-1]
    return (out.rstrip() + "…") if out else ""


def clean_condition(text: Optional[str], cap: int = 24) -> str:
    """Lowercase + de-verbose a provider condition string, capped to `cap` chars.

    'Showers And Thunderstorms Likely' -> 't-storms likely';
    'Chance Showers And Thunderstorms' -> 'chance t-storms'.
    """
    if not text:
        return ""
    s = " ".join(text.lower().split())
    # NOAA "X then Y" forecasts: keep the imminent condition X only (the later
    # transition just bloats and truncates the mesh line).
    if " then " in s:
        s = s.split(" then ", 1)[0].strip()
    for a, b in _COND_REWRITES:
        s = s.replace(a, b)
    s = " ".join(s.split())  # collapse again (rewrites can leave doubles)
    if len(s) > cap:
        s = s[:cap].rstrip() + "…"
    return s


def format_wind(direction: Optional[str], speed, *, calm_below: int = 3) -> str:
    """'SSW 10', or 'calm' when there's no meaningful wind."""
    try:
        spd = int(speed)
    except (TypeError, ValueError):
        spd = 0
    if spd < calm_below or not direction:
        return "calm"
    return f"{direction} {spd}"


# Magnus/August-Roche-Magnus constants for dew-point ↔ RH conversion.
_MAGNUS_A, _MAGNUS_B = 17.625, 243.04


def dewpoint_f(temp_f, rh):
    """Dew point (°F) from temperature (°F) + relative humidity (%); None if invalid.

    Lets the wx current block fill a missing dew point when the obs station reports
    RH but not dew point (or vice-versa via rh_from_dewpoint_f), instead of dropping it.
    """
    try:
        rh = float(rh)
        if rh <= 0:
            return None
        tc = (float(temp_f) - 32.0) * 5.0 / 9.0
        gamma = math.log(rh / 100.0) + (_MAGNUS_A * tc) / (_MAGNUS_B + tc)
        dew_c = (_MAGNUS_B * gamma) / (_MAGNUS_A - gamma)
        return int(round(dew_c * 9.0 / 5.0 + 32.0))
    except (TypeError, ValueError):
        return None


def rh_from_dewpoint_f(temp_f, dew_f):
    """Relative humidity (%) from temperature (°F) + dew point (°F); None if invalid."""
    try:
        tc = (float(temp_f) - 32.0) * 5.0 / 9.0
        dc = (float(dew_f) - 32.0) * 5.0 / 9.0
        rh = 100.0 * math.exp(
            (_MAGNUS_A * dc) / (_MAGNUS_B + dc) - (_MAGNUS_A * tc) / (_MAGNUS_B + tc)
        )
        return max(0, min(100, int(round(rh))))
    except (TypeError, ValueError):
        return None


def _temps_line(temp, unit: str, feels, hi, lo) -> str:
    """Line 2: 'now 86°F  feels 88°  H88° L72°' (only the lead temp carries the unit)."""
    out = f"now {temp}{unit}"
    if feels is not None and temp is not None:
        ft, tt = int(feels), int(temp)
        # Only when it intensifies: heat index (hotter) or wind chill (colder, cold weather).
        if ft >= tt + 2 or (ft <= tt - 2 and tt <= 50):
            out += f"  feels {ft}°"
    if hi is not None and lo is not None:
        out += f"  H{int(hi)}° L{int(lo)}°"
    elif hi is not None:
        out += f"  H{int(hi)}°"
    elif lo is not None:
        out += f"  L{int(lo)}°"
    return out


def _metrics_line(wind: Optional[str], rh, dew, uv) -> str:
    """Line 3: 'wind SSW 10 | RH 73% | dew 69° | UV 7' (each part conditional)."""
    parts = []
    if wind:
        parts.append(f"wind {wind}")
    if rh is not None:
        parts.append(f"RH {int(rh)}%")
    if dew is not None:
        parts.append(f"dew {int(dew)}°")
    if uv is not None and int(uv) > 2:   # omit UV at night / heavy overcast
        parts.append(f"UV {int(uv)}")
    return " | ".join(parts)


def _precip_tail(precip, *, floor: int = 15) -> str:
    if precip is None:
        return ""
    try:
        p = int(precip)
    except (TypeError, ValueError):
        return ""
    return f" {p}%" if p >= floor else ""


def current_block(loc, emoji, cond, precip, temp, *, unit="°F",
                  feels=None, hi=None, lo=None, wind=None, rh=None, dew=None, uv=None,
                  budget=158) -> str:
    """The 3-line current-conditions block, byte-budget aware."""
    l2 = _temps_line(temp, unit, feels, hi, lo)
    l3 = _metrics_line(wind, rh, dew, uv)
    body = l2 + ("\n" + l3 if l3 else "")
    tail = _precip_tail(precip)
    head = f"{loc} {emoji} " if emoji else f"{loc} "
    avail = budget - _bytes(body) - 1 - _bytes(head) - _bytes(tail)  # bytes left for the condition
    cond_c = clean_condition(cond)
    if _bytes(cond_c) > max(avail, 0):
        cond_c = _ellipsize(cond_c, max(avail, 0))
    l1 = (head + cond_c).rstrip() + tail
    block = l1 + "\n" + body
    # Final safety clamp: a pathological location could still overflow line 1.
    if _bytes(block) > budget:
        l1 = _ellipsize(l1, budget - _bytes(body) - 1)
        block = l1 + "\n" + body
    return block


def tomorrow_block(loc, day, emoji, cond, hi, lo, precip, wind, night_cond, *, budget=158) -> str:
    """The 3-line tomorrow block."""
    hdr = f"{loc} · Tomorrow {day}".rstrip()
    hl = ""
    if hi is not None and lo is not None:
        hl = f"  H{int(hi)}° L{int(lo)}°"
    pp = _precip_tail(precip)
    l2 = f"{emoji} {clean_condition(cond)}{hl}{pp}".strip()
    parts = []
    if wind:
        parts.append(f"wind {wind}")
    if night_cond:
        parts.append(f"night: {clean_condition(night_cond)}")
    l3 = " | ".join(parts)
    out = hdr + "\n" + l2 + ("\n" + l3 if l3 else "")
    # Safety: if a long night condition overflows, drop it.
    if _bytes(out) > budget and night_cond:
        out = hdr + "\n" + l2 + ("\n" + f"wind {wind}" if wind else "")
    return out


def day_row(day, emoji, cond, hi, lo) -> str:
    """One 7-day row: 'Mon 🌦️ t-storms  85°/72°' (condition kept short)."""
    hl = ""
    if hi is not None and lo is not None:
        hl = f"  {int(hi)}°/{int(lo)}°"
    elif hi is not None:
        hl = f"  {int(hi)}°"
    return f"{day} {emoji} {clean_condition(cond, cap=16)}{hl}".rstrip()


def hourly_row(time_label, emoji, temp, precip=None) -> str:
    """One hourly row (style A): '4PM 🌦️ 83° 41%'."""
    out = f"{time_label} {emoji} {int(temp)}°"
    if precip is not None and int(precip) >= 15:
        out += f" {int(precip)}%"
    return out


def pack(header: str, rows: list, *, budget=158, sep="\n") -> list:
    """Greedily pack header+rows into <=budget-byte messages.

    The header goes on the first message; continuation messages carry rows only.
    """
    msgs = []
    cur = header
    for r in rows:
        cand = cur + sep + r if cur else r
        if _bytes(cand) <= budget:
            cur = cand
        else:
            if cur:
                msgs.append(cur)
            cur = r
    if cur:
        msgs.append(cur)
    return msgs


# --- NWS alerts (shared, pure) -----------------------------------------------
# The proactive alert push renders with these so the look matches wx/gwx and the
# rules (canonical event name, boilerplate-drop, link-always-whole) live in one
# tested place. The event name MUST come from the feed's canonical cap:event.

_SEV_EMOJI = {"Extreme": "🔴", "Severe": "🟠", "Moderate": "🟡", "Minor": "⚪", "Unknown": "⚪"}

# Words that add nothing beyond the event name + its timing — used to decide if an
# NWS headline is pure boilerplate ("FLOOD WATCH IN EFFECT THROUGH MONDAY…").
_ALERT_BOILER = {
    "in", "effect", "remains", "remain", "is", "are", "be", "now", "will", "expire",
    "expires", "expired", "has", "have", "been", "issued", "the", "a", "an", "for",
    "of", "and", "or", "to", "at", "from", "through", "thru", "until", "till", "this",
    "that", "these", "those", "area", "areas", "portions", "county", "counties",
    "evening", "morning", "afternoon", "tonight", "night", "today", "tomorrow",
    "noon", "midnight", "am", "pm", "est", "edt", "cst", "cdt", "mst", "mdt", "pst",
    "pdt", "akst", "akdt", "hst", "utc", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}


def alert_emoji(severity: Optional[str]) -> str:
    """Severity → colored dot (🔴 Extreme / 🟠 Severe / 🟡 Moderate / ⚪ Minor·Unknown)."""
    return _SEV_EMOJI.get(severity or "", "⚪")


def alert_location(area_desc: Optional[str], *, home: Optional[str] = None,
                   neighbors=None, max_names: int = 4, max_chars: int = 44) -> str:
    """Build the alert's location label.

    With `home` (the bot's county, e.g. 'Davidson') + `neighbors` (set of adjacent
    county names): NAME the affected counties that are home-or-adjacent — home first,
    up to `max_names` and within `max_chars` — and collapse everything else (far
    counties + overflow) into '+N more', so the channel sees the relevant nearby
    counties instead of an alphabetical first area or a wall of names. Without that
    context (or non-county areas like marine zones), falls back to the first area
    + '+N more', ellipsized so a long zone can't blow the line.
    """
    if not area_desc:
        return ""
    entries = [x.strip() for x in area_desc.split(";") if x.strip()]
    if not entries:
        return ""
    home = (home or "").strip()
    nbrs = {n.strip() for n in (neighbors or ()) if n and n.strip()}

    def _legacy() -> str:
        first = entries[0]
        if len(first) > max_chars:
            first = first[: max_chars - 1].rstrip() + "…"
        return first + (f" +{len(entries) - 1} more" if len(entries) > 1 else "")

    if not home and not nbrs:
        return _legacy()

    # Classify each area into a distinct county: home, a neighbor, or "other" (far).
    def _match(entry: str):
        el = entry.lower()
        if home and home.lower() in el:
            return "home", home
        for n in nbrs:
            if n.lower() in el:
                return "nbr", n
        return "other", entry.split(",")[0].strip()

    local: list = []     # local county names, home first, deduped
    seen: set = set()
    others: set = set()  # distinct far-county keys
    for e in entries:
        kind, name = _match(e)
        if kind == "other":
            others.add(name.lower())
        elif name.lower() not in seen:
            seen.add(name.lower())
            local.insert(0, name) if kind == "home" else local.append(name)

    if not local:        # nothing local matched (unexpected when polling our own zone)
        return _legacy()

    shown: list = []
    for name in local:
        if len(shown) >= max_names or len(", ".join(shown + [name])) > max_chars:
            break
        shown.append(name)

    remaining = (len(local) - len(shown)) + len(others)
    out = ", ".join(shown)
    if remaining > 0:
        out += f" +{remaining} more"
    return out


# Phrases that mark a headline as real action/impact text worth keeping (checked
# AFTER the event name is removed, so "Tornado Warning…in effect" stays boilerplate).
_ALERT_ACTION_KW = (
    "take cover", "higher ground", "seek shelter", "life-threat", "life threat",
    "damaging", "destructive", "evacuat", "turn around", "move to", "dangerous",
    "catastrophic", "large hail", "considerable", "do not", "avoid", "located near",
    "impact", "moving", "mph", "knots", "flooding ongoing", "significant",
)


def headline_is_boilerplate(headline: Optional[str], event: str) -> bool:
    """True when the NWS headline adds nothing past the event name + timing/effect words.

    'FLOOD WATCH IN EFFECT THROUGH MONDAY' -> True (drop); 'SEVERE T-STORM WARNING
    REMAINS IN EFFECT UNTIL 11 PM FOR <counties>' -> True (drop); 'TAKE COVER NOW.
    A CONFIRMED TORNADO… MOVING NE AT 45 MPH' -> False (keep, real action text).
    """
    if not headline:
        return True
    h = headline.lower()
    for w in event.lower().split():
        h = h.replace(w, " ")
    has_action = any(k in h for k in _ALERT_ACTION_KW)
    # "in effect" is the NWS boilerplate marker — drop those unless they also carry
    # genuine action/impact text (storm tracking, take-cover language, etc.).
    if "in effect" in h and not has_action:
        return True
    rem_txt = re.sub(r"[^a-z ]+", " ", h)  # strip digits / punctuation / clock times
    rem = [w for w in rem_txt.split() if len(w) > 2 and w not in _ALERT_BOILER]
    return len(" ".join(rem)) < 12


def deshout(text: Optional[str]) -> str:
    """Calm an ALL-CAPS NWS string to Title Case; leave already-mixed-case text alone."""
    t = (text or "").strip()
    if not t:
        return ""
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return t.title()
    return t


def clean_headline(headline: Optional[str]) -> str:
    """De-shout + normalize an NWS headline to a clean one-line string for mesh.

    NWS delimits affected sub-areas/clauses with runs of dots
    ('...DAVIDSON...NORTHERN MAURY...'), which render as garbled text on the mesh.
    Collapse every run of 2+ dots/ellipses to a single ', ', squash newlines and
    whitespace, and fold any doubled or edge commas. Returns '' for empty /
    punctuation-only input. Single sentence periods (e.g. 'Take cover now.') are
    left intact — only runs of 2+ dots are treated as delimiters.
    """
    h = deshout(headline)
    if not h:
        return ""
    h = re.sub(r"\s*(?:\.{2,}|…+)\s*", ", ", h)    # NWS '...'/'…' area delimiters -> ', '
    h = re.sub(r"\s+", " ", h)                     # newlines / runs of whitespace -> one space
    h = re.sub(r"(?:,\s*)+", ", ", h)              # fold doubled commas (',,' / ', ,') -> ', '
    return h.strip(" ,")                           # trim stray leading/trailing comma/space


def alert_lines(severity, event, area_desc, expires, office, headline,
                *, home=None, neighbors=None, headline_cap: int = 120) -> list:
    """The labeled alert lines (no URL): 2-line core + an optional non-boilerplate headline.

      🟠 Flood Watch · Adams +7 more
      until Jun 8 1PM · NWS St Louis MO
      [Take cover now. A confirmed tornado…]   <- only when it adds info

    `home` (the bot's county) leads the area list when the alert includes it. For
    TORNADO watches/warnings the cap is lifted so EVERY adjacent affected county is
    named (home + neighbors), still collapsing non-adjacent ones to '+N more' — and
    pack_alert splits it across messages if the list overruns one line.
    """
    is_tornado = "tornado" in (event or "").lower()
    loc = alert_location(
        area_desc, home=home, neighbors=neighbors,
        max_names=(20 if is_tornado else 4),
        max_chars=(220 if is_tornado else 44),
    )
    l1 = f"{alert_emoji(severity)} {event}" + (f" · {loc}" if loc else "")
    l2 = " · ".join(p for p in (f"until {expires}" if expires else "", office or "") if p)
    lines = [l1] + ([l2] if l2 else [])
    if headline and not headline_is_boilerplate(headline, event):
        h = clean_headline(headline)
        if h:  # may be empty if the headline was only dots / punctuation
            if len(h) > headline_cap:
                cut = h[:headline_cap]
                brk = max(cut.rfind(". "), cut.rfind(", "))  # break on a sentence/area boundary
                h = (cut[:brk] if brk >= 40 else cut).rstrip(" .,") + "…"
            lines.append(h)
    return lines


def pack_alert(lines: list, url: str = "", *, budget: int = 144, cont: str = "…") -> list:
    """Pack alert lines into <=budget messages; the link is ALWAYS kept whole.

    Labeled lines are merged greedily and kept intact; a single line longer than
    budget is word-split (continuations prefixed with `cont`). The URL is attached
    WHOLE to the last message if it fits, else emitted as its own final message —
    never word-wrapped, split, or truncated (user-mandated link invariant).
    """
    body: list = []
    cur = ""
    for line in lines:
        if _bytes(line) <= budget:
            cand = f"{cur}\n{line}" if cur else line
            if _bytes(cand) <= budget:
                cur = cand
            else:
                if cur:
                    body.append(cur)
                cur = line
        else:  # a single line longer than budget — word-split it
            if cur:
                body.append(cur)
                cur = ""
            piece = ""
            for w in line.split():
                cand = f"{piece} {w}" if piece else w
                if _bytes(cand) <= budget:
                    piece = cand
                else:
                    if piece:
                        body.append(piece)
                    piece = w  # a lone word > budget is still kept whole
            cur = piece
    if cur:
        body.append(cur)
    if not body:
        body = [""]
    out = [c if i == 0 else f"{cont}{c}" for i, c in enumerate(body)]
    if url:
        if out[-1] and _bytes(f"{out[-1]} {url}") <= budget:
            out[-1] = f"{out[-1]} {url}"
        else:
            out.append(url)  # link whole on its own line — never split, even if over budget
    return out
