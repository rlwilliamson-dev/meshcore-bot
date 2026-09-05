"""Shared location-display helpers for the weather commands.

Originally lived in ``commands/rain_command.py``; lifted here so the wx (NOAA),
gwx (Open-Meteo), and rain/snow commands — plus the Weather_Service proactive
push — all label locations identically instead of each rolling its own. Pure
string helpers plus one reverse-geocoder; no command-class dependencies, so it
sits outside ``commands/`` (the plugin loader globs ``commands/*.py``).
"""
from typing import Any, Optional

from .utils import normalize_us_state


def titlecase_location(text: str) -> str:
    """Tidy a user-typed location for display.

    'middlesboro, ky' -> 'Middlesboro, KY'; 'paris, france' -> 'Paris, France';
    'memphis' -> 'Memphis'. A 2-letter token after a comma is treated as a
    state/country code and upper-cased; everything else is title-cased.
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return text.strip()
    out = []
    for i, p in enumerate(parts):
        if i > 0 and len(p) == 2 and p.isalpha():
            out.append(p.upper())
        else:
            out.append(p.title())
    return ", ".join(out)


# US state / territory 2-letter codes — used to drop a trailing state from a
# typed location like "london ky" (no comma) so it doesn't become "London Ky".
US_STATE_ABBRS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "AS", "GU", "MP", "PR", "VI",
})


def city_display_name(typed_location: str, suffix: Optional[str] = None) -> str:
    """City part of a typed location for display, dropping a trailing region the
    user appended without a comma.

    'london ky' -> 'London'; 'paris france' -> 'Paris'; 'london, ky' -> 'London';
    'oklahoma city' -> 'Oklahoma City'. `suffix` is the geocoder's authoritative
    state/country (e.g. 'KY' or 'France'); when the typed text ends with it, it's
    stripped so it isn't doubled into the city name. The state/country is added
    back separately by the caller.
    """
    head = typed_location.split(",")[0].strip()
    # Drop a trailing region matching the geocoder's suffix — handles country
    # names and multi-word regions ("paris france", "london united kingdom").
    if suffix and head.lower().endswith(" " + suffix.lower()):
        head = head[: -len(suffix)].strip()
    # Drop a trailing US state abbreviation ("london ky" -> "london").
    tokens = head.split()
    if len(tokens) >= 2 and tokens[-1].upper() in US_STATE_ABBRS:
        head = " ".join(tokens[:-1])
    return titlecase_location(head)


def join_location(city: Optional[str], suffix: Optional[str]) -> str:
    """Join a city and its state/country suffix as 'City, Suffix'.

    Collapses to a single name when one side is missing or the two name the same
    place (case-insensitive) — so a country typed as the city ('spain' -> 'Spain',
    not 'Spain, Spain') or a city-state ('Singapore', not 'Singapore, Singapore')
    renders once.
    """
    city = (city or "").strip()
    suffix = (suffix or "").strip()
    if not suffix:
        return city
    if not city or city.lower() == suffix.lower():
        return suffix
    return f"{city}, {suffix}"


_ZIP_CITY_CACHE: dict[str, str] = {}


def zip_to_city_string(zipcode: str, *, timeout: int = 10, logger: Any = None) -> Optional[str]:
    """US ZIP -> 'City, ST' via Zippopotam.us (free, no key, module-cached).

    OSM/Nominatim often lacks the USPS city for a ZIP centroid — it returns the
    county instead (e.g. 40965 -> "Bell County" rather than "Middlesboro") — so
    for 5-digit US ZIPs this gives a far better name. Returns None on failure so
    the caller can fall back to reverse geocoding. Shared by the wx and rain
    commands.
    """
    z = (zipcode or "").strip()
    if not z:
        return None
    if z in _ZIP_CITY_CACHE:
        return _ZIP_CITY_CACHE[z]
    name: Optional[str] = None
    try:
        import requests
        resp = requests.get(f"https://api.zippopotam.us/us/{z}", timeout=timeout)
        if resp.ok:
            places = resp.json().get("places") or []
            if places:
                city = (places[0].get("place name") or "").strip()
                st = (places[0].get("state abbreviation") or "").strip()
                if city:
                    name = join_location(city, st)
    except Exception as e:
        if logger is not None:
            logger.debug(f"Zippopotam ZIP lookup failed for {z}: {e}")
    if name:
        if len(_ZIP_CITY_CACHE) > 256:
            _ZIP_CITY_CACHE.clear()
        _ZIP_CITY_CACHE[z] = name
    return name


def region_suffix_from_address(address: dict) -> Optional[str]:
    """State/country suffix from a Nominatim address dict.

    Returns the US state abbreviation ('TN') for US points, else the country
    name. Prefers the ISO3166-2 subdivision code (e.g. 'US-TN' -> 'TN'), which
    is present even when the optional ``us`` library isn't installed; falls back
    to ``normalize_us_state`` then the raw state name. Shared so the rain command,
    the proactive push, and gwx all derive the same suffix from one place.
    """
    country_code = (address.get("country_code") or "").lower()
    if country_code == "us":
        iso = address.get("ISO3166-2-lvl4") or address.get("ISO3166-2-lvl6") or ""
        if "-" in iso:
            return iso.rsplit("-", 1)[-1]
        state_abbr, _ = normalize_us_state(address.get("state", ""))
        return state_abbr or address.get("state") or None
    return address.get("country") or None


def reverse_geocode_region(
    bot: Any, lat: float, lon: float, *, timeout: int = 10, logger: Any = None
) -> tuple[Optional[str], Optional[str]]:
    """Reverse-geocode to (city, suffix), respecting the bot's Nominatim rate limiter.

    suffix is the US state abbreviation ('TN') for US points, else the English
    country name ('Japan'). Requests language='en' so country names aren't
    localized. No caching (callers cache as needed). Shared by the rain command
    and the Weather_Service proactive push so both label locations identically.
    """
    city: Optional[str] = None
    suffix: Optional[str] = None
    try:
        from .utils import get_nominatim_geocoder
        limiter = getattr(bot, "nominatim_rate_limiter", None)
        if limiter is not None:
            limiter.wait_for_request_sync()
        geolocator = get_nominatim_geocoder(timeout=timeout)
        # language="en" so country names come back in English ("Japan", not "日本").
        result = geolocator.reverse(f"{lat}, {lon}", timeout=timeout, language="en")
        if limiter is not None:
            limiter.record_request()
        if result is not None and hasattr(result, "raw"):
            address = result.raw.get("address", {})
            city = (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("municipality")
                or address.get("county")
                or None
            )
            suffix = region_suffix_from_address(address)
    except Exception as e:
        if logger:
            logger.debug(f"Error reverse geocoding {lat},{lon}: {e}")
    return city, suffix
