#!/usr/bin/env python3
"""
Weather command for the MeshCore Bot
Provides weather information using zip codes and NOAA APIs
"""

import re
import xml.dom.minidom
from datetime import datetime, timedelta
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..location_format import join_location, region_suffix_from_address, zip_to_city_string
from ..models import MeshMessage
from ..region_capitals import REGION_DEFAULT_NOTE, region_capital_lookup
from ..utils import (
    format_temperature_high_low,
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
)
from ..wx_format import (
    current_block,
    day_row,
    dewpoint_f,
    format_wind,
    hourly_row,
    pack,
    rh_from_dewpoint_f,
    tomorrow_block,
)
from .base_command import BaseCommand

# Import for delegation when using Open-Meteo provider
try:
    from .alternatives.wx_international import GlobalWxCommand
    WX_INTERNATIONAL_AVAILABLE = True
except ImportError:
    WX_INTERNATIONAL_AVAILABLE = False
    GlobalWxCommand = None

# Import WXSIM parser for custom weather sources
try:
    from ..clients.wxsim_parser import WXSIMParser
    WXSIM_PARSER_AVAILABLE = True
except ImportError:
    WXSIM_PARSER_AVAILABLE = False
    WXSIMParser = None

from ..clients.mqtt_weather import (
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)

# Multiday: plain digits (e.g. 7), 7day/7-day, or suffix form 7d/10d (min 2, max below).
WX_MULTIDAY_MAX_DAYS = 16


class WxCommand(BaseCommand):
    """Handles weather commands with zipcode support"""

    # Plugin metadata
    name = "wx"
    keywords = ['wx', 'weather', 'wxa', 'wxalert']
    description = "US weather (NOAA) for a city, ZIP, or coordinates (use gwx outside the US)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    # NOAA/geocoding need the network, but custom WXSIM/MQTT sources may be LAN-only; check connectivity inside execute paths.
    requires_internet = False

    # Documentation
    short_description = "Get weather for a US location using NOAA weather data"
    usage = "wx <city|ZIP|lat,lon> [tomorrow|Nd|hourly|alerts]"
    examples = ["wx nashville", "wx 37013 7d", "wx seattle hourly"]
    parameters = [
        {"name": "location", "description": "city, US ZIP, or lat,lon"},
        {"name": "option", "description": "tomorrow, Nd (e.g. 7d, 10d), hourly, or alerts (optional)"}
    ]

    # Error constants
    NO_DATA_NOGPS = "No GPS data available"
    ERROR_FETCHING_DATA = "Error fetching weather data"
    NO_ALERTS = "No weather alerts"

    def __init__(self, bot):
        super().__init__(bot)
        self.wx_enabled = self.get_config_value('Wx_Command', 'enabled', fallback=True, value_type='bool')

        # Initialize WXSIM parser if available
        if WXSIM_PARSER_AVAILABLE:
            self.wxsim_parser = WXSIMParser()
        else:
            self.wxsim_parser = None

        # Check weather provider setting - delegate to international command if using Open-Meteo
        weather_provider = bot.config.get('Weather', 'weather_provider', fallback='noaa').lower()
        if weather_provider == 'openmeteo' and WX_INTERNATIONAL_AVAILABLE:
            # Delegate to international weather command
            self.delegate_command = GlobalWxCommand(bot)
            # Update keywords to match wx command for compatibility
            self.delegate_command.keywords = ['wx', 'weather', 'wxa', 'wxalert']
            self.delegate_command.description = "Get weather information for any location (usage: wx Tokyo)"
            self.logger.info("Weather provider set to 'openmeteo', delegating wx command to wx_international")
        else:
            self.delegate_command = None

        # Only initialize NOAA-specific attributes if not delegating
        if self.delegate_command is None:
            self.url_timeout = 8  # seconds (reduced from 10 for faster failure detection)
            self.forecast_duration = 3  # days
            self.num_wx_alerts = 2  # number of alerts to show
            self.use_metric = False  # Use imperial units by default
            self.zulu_time = False  # Use local time by default

            # Get default location/state/country from config for fallback/disambiguation
            self.default_city = self.bot.config.get('Weather', 'default_city', fallback='').strip()
            self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
            self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            self._alert_zone_cache: dict = {}  # (lat,lon)->NWS county zone for alert queries

            # Initialize geocoder (will use rate-limited helpers for actual calls)
            # Keep geolocator for backwards compatibility, but prefer rate-limited helpers
            self.geolocator = get_nominatim_geocoder()

            # Get database manager for geocoding cache
            self.db_manager = bot.db_manager

            # Create a retry-enabled session for NOAA API calls
            # This makes the API more resilient to timeouts and transient errors
            self.noaa_session = self._create_retry_session()

    def _format_high_low(self, high: Optional[float], low: Optional[float], temp_symbol: str) -> str:
        """Format high/low using [Weather] temperature_*_format templates."""
        return format_temperature_high_low(self.bot.config, high, low, temp_symbol, self.logger)

    @staticmethod
    def _noaa_period_temp_symbol(period: dict) -> str:
        u = (period.get("temperatureUnit") or "F").upper()
        return "°F" if u == "F" else "°C"

    def _create_retry_session(self) -> requests.Session:
        """Create a requests session with retry logic for NOAA API calls"""
        session = requests.Session()

        # Configure retry strategy
        # Retry on: connection errors, timeout errors, and 5xx server errors
        # Reduced to 2 retries (total 3 attempts) for faster failure recovery
        retry_strategy = Retry(
            total=2,  # Total number of retries (3 total attempts: 1 initial + 2 retries)
            backoff_factor=0.3,  # Wait 0.3s, 0.6s between retries (faster backoff)
            status_forcelist=[500, 502, 503, 504],  # Retry on these HTTP status codes
            allowed_methods=["GET"],  # Only retry GET requests
            raise_on_status=False  # Don't raise exception on status codes, let us handle it
        )

        # Mount the adapter with connection pooling for better performance
        # pool_connections: number of connection pools to cache
        # pool_maxsize: maximum number of connections to save in the pool
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,  # Reuse connections for better performance
            pool_maxsize=20
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def get_help_text(self) -> str:
        """Get help text, delegating to international command if using Open-Meteo"""
        if self.delegate_command:
            return self.delegate_command.get_help_text()
        return self.translate('commands.wx.description')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a weather keyword"""
        if self.delegate_command:
            return self.delegate_command.matches_keyword(message)

        content_lower = self.cleanup_message_for_matching(message)
        return any(content_lower.startswith(keyword + ' ') or content_lower == keyword for keyword in self.keywords)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Override to delegate or use base class cooldown"""
        # Check if wx command is enabled
        if not self.wx_enabled:
            return False

        if self.delegate_command:
            # Enforce [Wx_Command] channels first; delegate uses skip_channel_check
            # so [Wx_Command] channels override is honored when using Open-Meteo
            if not self.is_channel_allowed(message):
                return False
            return self.delegate_command.can_execute(message, skip_channel_check=True)

        # Use base class for cooldown and other checks
        return super().can_execute(message)

    def get_remaining_cooldown(self, user_id: Optional[str] = None) -> int:
        """Get remaining cooldown time for a specific user"""
        if self.delegate_command:
            return self.delegate_command.get_remaining_cooldown(user_id)

        # Use base class method
        return super().get_remaining_cooldown(user_id)

    def _get_companion_location(self, message: MeshMessage) -> Optional[tuple[float, float]]:
        """Get companion/sender location from database.

        Args:
            message: The message object.

        Returns:
            Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None.
        """
        try:
            sender_pubkey = message.sender_pubkey
            if not sender_pubkey:
                self.logger.debug("No sender_pubkey in message for companion location lookup")
                return None

            query = '''
                SELECT latitude, longitude
                FROM complete_contact_tracking
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''

            results = self.bot.db_manager.execute_query(query, (sender_pubkey,))

            if results:
                row = results[0]
                lat = row['latitude']
                lon = row['longitude']
                self.logger.debug(f"Found companion location: {lat}, {lon} for pubkey {sender_pubkey[:16]}...")
                return (lat, lon)
            else:
                self.logger.debug(f"No location found in database for pubkey {sender_pubkey[:16]}...")
            return None
        except Exception as e:
            self.logger.warning(f"Error getting companion location: {e}")
            return None

    def _get_bot_location(self) -> Optional[tuple[float, float]]:
        """Get bot location from config.

        Returns:
            Optional[Tuple[float, float]]: Tuple of (latitude, longitude) or None.
        """
        try:
            lat = self.bot.config.getfloat('Bot', 'bot_latitude', fallback=None)
            lon = self.bot.config.getfloat('Bot', 'bot_longitude', fallback=None)

            if lat is not None and lon is not None:
                # Validate coordinates
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return (lat, lon)
            return None
        except Exception as e:
            self.logger.debug(f"Error getting bot location: {e}")
            return None

    def _get_custom_wxsim_source(self, location: Optional[str] = None) -> Optional[str]:
        """Get custom WXSIM source URL from config.

        Looks for keys in [Weather] section with pattern: custom.wxsim.<name> = <url>
        Similar to how Channels_List handles dotted keys.

        Args:
            location: Location name or None for default source

        Returns:
            Optional[str]: Source URL or None if not found
        """
        if not self.wxsim_parser:
            self.logger.debug("WXSIM parser not available")
            return None

        section = 'Weather'
        if not self.bot.config.has_section(section):
            self.logger.debug(f"Config section '{section}' does not exist")
            return None

        if location:
            # Strip whitespace and normalize
            location = location.strip()
            location_lower = location.lower()
            self.logger.debug(f"Checking for WXSIM source for location: '{location}' (normalized: '{location_lower}')")

            # Look for keys matching custom.wxsim.<location> pattern
            prefix = 'custom.wxsim.'
            for key, value in self.bot.config.items(section):
                if key.startswith(prefix):
                    # Extract the location name from the key (e.g., "custom.wxsim.lethbridge" -> "lethbridge")
                    key_location = key[len(prefix):].strip()
                    if key_location.lower() == location_lower:
                        self.logger.debug(f"Found WXSIM source: {key} = {value}")
                        return value

            self.logger.debug(f"No WXSIM source found for location '{location}'")
        else:
            # Check for default source: custom.wxsim.default
            default_key = 'custom.wxsim.default'
            if self.bot.config.has_option(section, default_key):
                url = self.bot.config.get(section, default_key)
                self.logger.debug(f"Found default WXSIM source: {url}")
                return url
            self.logger.debug("No default WXSIM source configured")

        return None

    def _get_custom_mqtt_weather_topic(self, location: Optional[str] = None) -> Optional[str]:
        """MQTT topic for custom.mqtt_weather.<name> (see get_mqtt_weather_topic)."""
        return get_mqtt_weather_topic(self.bot.config, location)

    def _mqtt_weather_line(
        self,
        topic: str,
        forecast_type: str,
        location_name: Optional[str],
    ) -> str:
        """Format cached MQTT payload for wx output."""
        if forecast_type != "default":
            return self.translate("commands.wx.mqtt_forecast_not_supported")

        fmt = load_mqtt_weather_format_config(self.bot.config)
        cache = getattr(self.bot, "mqtt_weather_cache", None)
        text, err = mqtt_weather_display_for_topic(topic, cache, fmt)
        if text is not None:
            if location_name:
                return f"{location_name}: {text}"
            return text
        return self._mqtt_weather_error_key(err)

    def _mqtt_weather_error_key(self, err: Optional[str]) -> str:
        if err == "no_cache":
            return self.translate("commands.wx.mqtt_weather_no_subscriber")
        if err in ("no_data", "empty_payload", "empty_after_sanitize"):
            return self.translate("commands.wx.mqtt_weather_no_data")
        if err == "stale":
            return self.translate("commands.wx.mqtt_weather_stale")
        detail = (err or "unknown").replace("_", " ")
        return self.translate("commands.wx.mqtt_weather_payload_error", detail=detail)

    def _get_wxsim_weather(self, source_url: str, forecast_type: str = "default",
                                num_days: int = 7, message: MeshMessage = None,
                                location_name: Optional[str] = None) -> str:
        """Get and format weather from WXSIM source.

        Args:
            source_url: URL to WXSIM plaintext.txt file
            forecast_type: "default", "tomorrow", or "multiday"
            num_days: Number of days for multiday forecast
            message: The MeshMessage for dynamic length calculation
            location_name: Optional location name for display

        Returns:
            str: Formatted weather string
        """
        if not self.wxsim_parser:
            return self.translate('commands.wx.error', error="WXSIM parser not available")

        # Fetch WXSIM data
        text = self.wxsim_parser.fetch_from_url(source_url, timeout=self.url_timeout)
        if not text:
            return self.translate('commands.wx.error', error="Failed to fetch WXSIM data")

        # Parse the data
        forecast = self.wxsim_parser.parse(text)

        # Validate forecast is not stale
        is_stale, stale_reason = self.wxsim_parser.is_forecast_stale(forecast, max_age_hours=48)
        if is_stale:
            self.logger.warning(f"WXSIM forecast appears stale: {stale_reason}")
            # Still return the forecast, but log the warning
            # Optionally, we could return an error message here instead

        # Get unit preferences from config
        temp_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
        wind_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()

        # Format based on forecast type
        if forecast_type == "tomorrow":
            # Get tomorrow's forecast
            if len(forecast.periods) > 1:
                tomorrow = forecast.periods[1]
                high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp else None
                low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                result = f"Tomorrow: {tomorrow.conditions}"
                hl = self._format_high_low(high, low, temp_symbol)
                if hl:
                    result += f" {hl}"

                if tomorrow.precip_chance and tomorrow.precip_chance > 30:
                    result += f" {tomorrow.precip_chance}% PoP"

                if location_name:
                    return f"{location_name}: {result}"
                return result
            else:
                return self.translate('commands.wx.error', error="Tomorrow forecast not available")

        elif forecast_type == "multiday":
            # Format multiday forecast
            summary = self.wxsim_parser.format_forecast_summary(forecast, num_days, temp_unit, wind_unit)
            if location_name:
                return f"{location_name}:\n{summary}"
            return summary

        else:
            # Default: current conditions + today's forecast
            current = self.wxsim_parser.format_current_conditions(forecast, temp_unit, wind_unit)

            # Add today's high/low if available (use first period as "today")
            if forecast.periods:
                today = forecast.periods[0]
                high = self.wxsim_parser._convert_temp(today.high_temp, temp_unit) if today.high_temp else None
                low = self.wxsim_parser._convert_temp(today.low_temp, temp_unit) if today.low_temp else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                hl_today = self._format_high_low(high, low, temp_symbol)
                if hl_today:
                    current += f" | {hl_today}"

                # Add tomorrow if available (second period)
                if len(forecast.periods) > 1:
                    tomorrow = forecast.periods[1]
                    tomorrow_high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp else None
                    tomorrow_low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp else None

                    hl_tom = self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
                    if hl_tom:
                        current += f" | Tomorrow: {hl_tom}"

            if location_name:
                return f"{location_name}: {current}"
            return current

    def _coordinates_to_location_string(self, lat: float, lon: float) -> Optional[str]:
        """Convert coordinates to a location string (city name) using reverse geocoding.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            Optional[str]: Location string (city name) or None if geocoding fails.
        """
        try:
            from ..utils import rate_limited_nominatim_reverse_sync
            result = rate_limited_nominatim_reverse_sync(self.bot, f"{lat}, {lon}", timeout=10)
            if result and hasattr(result, 'raw'):
                # Extract city name from address
                address = result.raw.get('address', {})
                city = (address.get('city') or
                       address.get('town') or
                       address.get('village') or
                       address.get('municipality') or
                       address.get('county', ''))
                # State abbreviation (US) / country via the ISO3166-2 code, so it
                # works without the optional `us` library (not installed in prod).
                return join_location(city, region_suffix_from_address(address)) or None
            return None
        except Exception as e:
            self.logger.debug(f"Error reverse geocoding coordinates {lat}, {lon}: {e}")
            return None

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the weather command"""
        # Delegate to international command if using Open-Meteo provider
        if self.delegate_command:
            return await self.delegate_command.execute(message)

        content = message.content.strip()

        # Parse the command to extract location and forecast type
        # Support formats: "wx 12345", "wx seattle", "wx paris, tx", "weather everett", "wxa bellingham"
        # New formats: "wx 12345 tomorrow", "wx 12345 7", "wx 12345 7d", "wx 12345 7day", "wx 12345 alerts"
        parts = content.split()

        # Track if we're using companion location (so we always show location in response)
        using_companion_location = False

        # If no location specified, check custom MQTT then WXSIM default sources
        if len(parts) < 2:
            mqtt_topic = self._get_custom_mqtt_weather_topic(None)
            if mqtt_topic:
                try:
                    self.record_execution(message.sender_id)
                    weather_data = self._mqtt_weather_line(mqtt_topic, "default", None)
                    await self.send_response(message, weather_data)
                    return True
                except Exception as e:
                    self.logger.error(f"Error reading MQTT weather: {e}")
                    await self.send_response(message, self.translate("commands.wx.error", error=str(e)))
                    return True

            wxsim_source = self._get_custom_wxsim_source(None)  # Check for default
            if wxsim_source:
                # Use custom WXSIM default source
                try:
                    self.record_execution(message.sender_id)
                    weather_data = self._get_wxsim_weather(wxsim_source, "default", 7, message)
                    await self.send_response(message, weather_data)
                    return True
                except Exception as e:
                    self.logger.error(f"Error fetching WXSIM weather: {e}")
                    await self.send_response(message, self.translate('commands.wx.error', error=str(e)))
                    return True

            # No custom source, try companion location
            companion_location = self._get_companion_location(message)
            if companion_location:
                # Use coordinates directly to avoid re-geocoding issues
                location_str = f"{companion_location[0]},{companion_location[1]}"
                parts = [parts[0], location_str]
                using_companion_location = True
                # Get city name for display
                display_name = self._coordinates_to_location_string(companion_location[0], companion_location[1])
                if display_name:
                    self.logger.info(f"Using companion location: {display_name} ({companion_location[0]}, {companion_location[1]})")
                else:
                    self.logger.info(f"Using companion coordinates: {location_str}")
            else:
                # No companion location: use default city if configured, then bot location fallback
                if self.default_city:
                    location_parts = [self.default_city]
                    if self.default_state:
                        location_parts.append(self.default_state)
                    if self.default_country:
                        location_parts.append(self.default_country)
                    location_str = ", ".join(location_parts)
                    parts = [parts[0], location_str]
                    self.logger.info(f"Using default city (no args): {location_str}")
                else:
                    # No default city: optionally use bot's configured coordinates
                    use_bot = self.get_config_value(
                        'Wx_Command',
                        'use_bot_location_when_no_location',
                        fallback=False,
                        value_type='bool',
                    )
                    bot_loc = self._get_bot_location() if use_bot else None
                    if bot_loc:
                        location_str = f"{bot_loc[0]},{bot_loc[1]}"
                        parts = [parts[0], location_str]
                        display_name = self._coordinates_to_location_string(bot_loc[0], bot_loc[1])
                        if display_name:
                            self.logger.info(
                                f"Using bot location (no args): {display_name} ({bot_loc[0]}, {bot_loc[1]})"
                            )
                        else:
                            self.logger.info(f"Using bot coordinates (no args): {location_str}")
                    else:
                        if use_bot:
                            self.logger.debug(
                                "use_bot_location_when_no_location enabled but bot_latitude/bot_longitude "
                                "not set; showing usage"
                            )
                        else:
                            self.logger.debug("No companion/default city location found, showing usage")
                        await self.send_response(message, self.translate('commands.wx.usage'))
                        return True

        # Check for an "alert"/"alerts" keyword first (special handling). Accept
        # singular + plural, and a BARE "wx alerts" (no location) -> default city
        # below, instead of geocoding "alert(s)" as a place name.
        show_full_alerts = False
        if len(parts) > 1 and parts[-1].lower() in ("alert", "alerts"):
            show_full_alerts = True
            location_parts = parts[1:-1]  # may be empty -> default city (handled below)
        else:
            location_parts = parts[1:]

        # Check for forecast type options: "tomorrow", Nd (7d, 10d), or plain digit days 2–WX_MULTIDAY_MAX_DAYS
        forecast_type = "default"
        num_days = 7  # Default for multi-day forecast

        # Check last part for forecast type (only if not "alerts")
        if len(location_parts) > 0 and not show_full_alerts:
            last_part = location_parts[-1].lower()
            if last_part == "tomorrow":
                forecast_type = "tomorrow"
                location_parts = location_parts[:-1]
            elif last_part == "hourly":
                forecast_type = "hourly"
                location_parts = location_parts[:-1]
            elif last_part in ["7day", "7-day"]:
                forecast_type = "multiday"
                num_days = 7
                location_parts = location_parts[:-1]
            else:
                nd_match = re.fullmatch(r"(\d+)d", last_part)
                if nd_match:
                    days = int(nd_match.group(1))
                    if 2 <= days <= WX_MULTIDAY_MAX_DAYS:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]
                elif last_part.isdigit():
                    days = int(last_part)
                    if 2 <= days <= WX_MULTIDAY_MAX_DAYS:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]

        # Join remaining parts to handle "city, state" format
        location = ' '.join(location_parts).strip()

        if not location:
            # Bare "wx alert(s)" -> alerts for the default city, not usage.
            if show_full_alerts and self.default_city:
                location = self.default_city + (f", {self.default_state}" if self.default_state else "")
            else:
                await self.send_response(message, self.translate('commands.wx.usage'))
                return True

        # "wx help" / "wx ?" -> show usage, don't geocode "help" as a place.
        if location.lower() in ("help", "?", "-h", "--help"):
            await self.send_response(message, self.translate('commands.wx.usage'))
            return True

        # Bare US state (e.g. "tennessee") -> its capital, with a heads-up note,
        # since one state-centroid point isn't representative. Bare foreign country
        # (e.g. "france") -> redirect to gwx, since NOAA only covers the US. A comma
        # means the user already named a city, so this returns (None, None) and we
        # fall through to normal geocoding ("paris, france" is handled below).
        region_note: Optional[str] = None
        cap_query, cap_kind = region_capital_lookup(location)
        if cap_kind == "country":
            await self.send_response(
                message, self.translate('commands.wx.redirect_gwx', location=location)
            )
            return True
        if cap_kind == "state":
            location = cap_query
            # Only annotate the single-message default forecast; multiday/alerts
            # have no room and already show the resolved capital in their prefix.
            if forecast_type == "default" and not show_full_alerts:
                region_note = REGION_DEFAULT_NOTE

        # Custom MQTT before WXSIM; skip snapshot sources when user asked for NOAA alerts
        if not show_full_alerts:
            mqtt_topic = self._get_custom_mqtt_weather_topic(location)
            if mqtt_topic:
                self.logger.info(f"Using custom MQTT weather topic for location '{location}': {mqtt_topic}")
                try:
                    self.record_execution(message.sender_id)
                    weather_data = self._mqtt_weather_line(
                        mqtt_topic, forecast_type, location
                    )
                    if forecast_type == "multiday":
                        await self._send_multiday_forecast(message, weather_data)
                    else:
                        await self.send_response(message, weather_data)
                    return True
                except Exception as e:
                    self.logger.error(f"Error reading MQTT weather: {e}")
                    await self.send_response(message, self.translate("commands.wx.error", error=str(e)))
                    return True

        # Check for custom WXSIM source first (before checking location type)
        wxsim_source = self._get_custom_wxsim_source(location)
        if wxsim_source:
            self.logger.info(f"Using custom WXSIM source for location '{location}': {wxsim_source}")
            # Use custom WXSIM source
            try:
                self.record_execution(message.sender_id)
                weather_data = self._get_wxsim_weather(wxsim_source, forecast_type, num_days, message, location_name=location)
                if forecast_type == "multiday":
                    await self._send_multiday_forecast(message, weather_data)
                else:
                    await self.send_response(message, weather_data)
                return True
            except Exception as e:
                self.logger.error(f"Error fetching WXSIM weather: {e}")
                await self.send_response(message, self.translate('commands.wx.error', error=str(e)))
                return True
        else:
            self.logger.debug(f"No custom WXSIM source found for location '{location}', using normal weather API")

        # Check if it's coordinates, zipcode, or city name
        if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
            # It's coordinates (lat,lon format)
            location_type = "coordinates"
        elif re.match(r'^\d{5}$', location):
            # It's a zipcode
            location_type = "zipcode"
        else:
            # It's a city name (possibly with state)
            location_type = "city"

        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Special handling for "alerts" command
            if show_full_alerts:
                # Get alerts only (no weather forecast)
                lat, lon = None, None
                if location_type == "coordinates":
                    try:
                        lat_str, lon_str = location.split(',')
                        lat = float(lat_str.strip())
                        lon = float(lon_str.strip())
                        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                            await self.send_response(message, self.translate('commands.wx.error', error="Invalid coordinates"))
                            return True
                    except ValueError:
                        await self.send_response(message, self.translate('commands.wx.error', error=f"Invalid coordinates format: {location}"))
                        return True
                elif location_type == "zipcode":
                    lat, lon = self.zipcode_to_lat_lon(location)
                    if lat is None or lon is None:
                        await self.send_response(message, self.translate('commands.wx.no_location_zipcode', location=location))
                        return True
                else:  # city
                    result = self.city_to_lat_lon(location)
                    if len(result) == 3:
                        lat, lon, address_info = result
                    else:
                        lat, lon = result
                    if lat is None or lon is None:
                        region = self.default_state or self.default_country
                        await self.send_response(message, self.translate('commands.wx.no_location_city', location=location, state=region))
                        return True

                # Get and display full alert list
                await self._send_full_alert_list(message, lat, lon)
                return True

            # Get weather data for the location. Reserve byte budget up front for
            # the region note so appending it below can't overflow the radio frame.
            note_reserve = len((" " + region_note).encode("utf-8")) if region_note else 0
            weather_data = await self.get_weather_for_location(location, location_type, forecast_type, num_days, message, using_companion_location=using_companion_location, reserve=note_reserve)

            # A foreign city geocoded to a non-US point -> NOAA can't serve it;
            # redirect to gwx (same message as a bare foreign country above).
            if isinstance(weather_data, tuple) and weather_data[0] == "redirect_gwx":
                await self.send_response(
                    message, self.translate('commands.wx.redirect_gwx', location=weather_data[1])
                )
                return True

            # Check if we need to send multiple messages
            if isinstance(weather_data, tuple) and weather_data[0] == "multi_message":
                # Send weather data first (with the region note if we resolved a capital)
                weather_text = weather_data[1]
                if region_note:
                    weather_text = f"{weather_text} {region_note}"
                await self.send_response(message, weather_text)

                # Gap before the alert message so the mesh doesn't flood (tunable via config).
                import asyncio
                delay = self.bot.config.getfloat('Bot', 'multi_message_delay_seconds', fallback=5.0)
                await asyncio.sleep(delay)

                # Send the special weather statement (already formatted with prioritization)
                alert_text = weather_data[2]
                await self.send_response(message, alert_text)
            elif forecast_type == "multiday":
                # Use message splitting for multi-day forecasts
                await self._send_multiday_forecast(message, weather_data)
            else:
                # Send single message as usual (appending the region note if set)
                if region_note:
                    weather_data = f"{weather_data} {region_note}"
                await self.send_response(message, weather_data)

            return True

        except Exception as e:
            self.logger.error(f"Error in weather command: {e}")
            await self.send_response(message, self.translate('commands.wx.error', error=str(e)))
            return True

    async def get_weather_for_location(self, location: str, location_type: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, using_companion_location: bool = False, reserve: int = 0) -> str:
        """Get weather data for a location (coordinates, zipcode, or city)

        Args:
            location: The location (coordinates "lat,lon", zipcode, or city name)
            location_type: "coordinates", "zipcode", or "city"
            forecast_type: "default", "tomorrow", "multiday", or "hourly"
            num_days: Number of days for multiday forecast (2–16)
            message: The MeshMessage for dynamic length calculation
            using_companion_location: If True, always include location prefix even if same state
            reserve: Bytes to hold back from the message budget (e.g. for an
                appended region note), so the caller's suffix can't overflow.

        Returns a formatted string, or a ("redirect_gwx", location) tuple when a
        non-US location is requested (NOAA can't serve it).
        """
        try:
            # Convert location to lat/lon based on type
            if location_type == "coordinates":
                # Parse coordinates from "lat,lon" format
                try:
                    lat_str, lon_str = location.split(',')
                    lat = float(lat_str.strip())
                    lon = float(lon_str.strip())

                    # Validate coordinate ranges
                    if not (-90 <= lat <= 90):
                        return self.translate('commands.wx.error', error=f"Invalid latitude: {lat}")
                    if not (-180 <= lon <= 180):
                        return self.translate('commands.wx.error', error=f"Invalid longitude: {lon}")

                    # Get address_info for location display via reverse geocoding
                    location_str = self._coordinates_to_location_string(lat, lon)
                    if location_str:
                        # Parse the location string to get city and state for address_info
                        parts = location_str.split(',')
                        if len(parts) >= 2:
                            city = parts[0].strip()
                            state = parts[1].strip()
                            address_info = {'city': city, 'state': state}
                        else:
                            address_info = {'city': location_str}
                    else:
                        address_info = {}
                except ValueError:
                    return self.translate('commands.wx.error', error=f"Invalid coordinates format: {location}")
            elif location_type == "zipcode":
                lat, lon = self.zipcode_to_lat_lon(location)
                if lat is None or lon is None:
                    return self.translate('commands.wx.no_location_zipcode', location=location)
                address_info = None
            else:  # city
                result = self.city_to_lat_lon(location)
                if len(result) == 3:
                    lat, lon, address_info = result
                else:
                    lat, lon = result
                    address_info = None

                if lat is None or lon is None:
                    region = self.default_state or self.default_country
                    return self.translate('commands.wx.no_location_city', location=location, state=region)

                # A city that geocoded to a non-US country -> NOAA can't serve it;
                # signal the caller to redirect the user to the global gwx command.
                country_code = (address_info or {}).get('country_code', '').lower()
                if country_code and country_code != 'us':
                    return ("redirect_gwx", location)

                # Check if the found city is in a different state than default
                actual_city = location
                actual_state = self.default_state or self.default_country
                if address_info:
                    # Try to get the best city name from various address fields
                    actual_city = (address_info.get('city') or
                                 address_info.get('town') or
                                 address_info.get('village') or
                                 address_info.get('hamlet') or
                                 address_info.get('municipality') or
                                 location)
                    actual_state = address_info.get('state', self.default_state)
                    # Convert full state name to abbreviation if needed using the us library
                    if len(actual_state) > 2:
                        state_abbr, _ = normalize_us_state(actual_state)
                        if state_abbr:
                            actual_state = state_abbr

                    # Also check if the default state needs to be converted for comparison
                    default_state_full = self.default_state
                    if len(self.default_state) == 2:
                        # Convert abbreviation to full name for comparison
                        _, default_state_full = normalize_us_state(self.default_state)
                        if not default_state_full:
                            default_state_full = self.default_state

            # Location label shown on every reply — the new format embeds it on
            # line 1 (current) or the header (tomorrow/7-day/hourly).
            loc_label = location
            if location_type == "coordinates" and address_info:
                city = address_info.get('city', '')
                state = address_info.get('state', '')
                if state:
                    state_abbr, _ = normalize_us_state(state)
                    if state_abbr:
                        state = state_abbr
                loc_label = join_location(city, state) or location
            elif location_type == "city" and address_info:
                # Robust state suffix via the ISO3166-2 code (works without the `us` lib).
                display_suffix = region_suffix_from_address(address_info) or actual_state
                loc_label = join_location(actual_city, display_suffix) or location
            elif location_type == "zipcode":
                # USPS city first (Zippopotam) — Nominatim returns the county for
                # rural ZIP centroids (e.g. 40965 -> "Bell County"); fall back to
                # reverse geocoding only if that lookup fails.
                zip_city = (zip_to_city_string(location, timeout=self.url_timeout, logger=self.logger)
                            or self._coordinates_to_location_string(lat, lon))
                loc_label = f"{zip_city} ({location})" if zip_city else location

            # Get max message length dynamically, holding back any reserved bytes
            max_length = (self.get_max_message_length(message) if message else 130) - reserve

            # Get weather forecast based on type
            if forecast_type == "tomorrow":
                forecast_periods, points_data = self.get_noaa_weather(lat, lon, return_periods=True, max_length=max_length)
                if forecast_periods == self.ERROR_FETCHING_DATA:
                    return self.translate('commands.wx.error_fetching')
                weather = self.format_tomorrow_forecast(forecast_periods, max_length=max_length, location=loc_label)
            elif forecast_type == "multiday":
                forecast_periods, points_data = self.get_noaa_weather(lat, lon, return_periods=True, max_length=max_length)
                if forecast_periods == self.ERROR_FETCHING_DATA:
                    return self.translate('commands.wx.error_fetching')
                weather = self.format_multiday_forecast(forecast_periods, num_days, max_length=max_length, location=loc_label)
            elif forecast_type == "hourly":
                hourly_periods, points_data = self.get_noaa_hourly_weather(lat, lon)
                if hourly_periods == self.ERROR_FETCHING_DATA:
                    return self.translate('commands.wx.error_fetching')
                weather = self.format_hourly_forecast(hourly_periods, max_length=max_length, location=loc_label)
            else:  # default
                weather, points_data = self.get_noaa_weather(lat, lon, max_length=max_length, location=loc_label)
                if weather == self.ERROR_FETCHING_DATA:
                    return self.translate('commands.wx.error_fetching')

                # Note: Current conditions are now integrated directly into the current period
                # via _add_period_details() using observation station data

            # Append active NWS alerts to current conditions — OFF by default: the
            # proactive Weather_Service push already covers the channel, and during a
            # multi-hour watch this would re-send it on every wx query. Set
            # [Wx_Command] append_alerts = true to opt in; "wx <place> alerts" always
            # shows them on demand regardless.
            if forecast_type == "default" and self.get_config_value(
                'Wx_Command', 'append_alerts', fallback=False, value_type='bool'
            ):
                alerts_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=False)
                if alerts_result == self.ERROR_FETCHING_DATA or alerts_result == self.NO_ALERTS:
                    pass
                else:
                    full_alert_text, abbreviated_alert_text, alert_count = alerts_result
                    if alert_count > 0:
                        # Get full alert data for prioritized formatting
                        alerts_full_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=True)
                        if alerts_full_result not in [self.ERROR_FETCHING_DATA, self.NO_ALERTS]:
                            alerts_list, _ = alerts_full_result
                            # Format with prioritization and summary
                            formatted_alert_text = self._format_alerts_compact_summary(alerts_list, alert_count, max_length=max_length)
                        else:
                            # Fallback to old format
                            formatted_alert_text = full_alert_text

                        # Always send weather first, then alerts in separate message
                        self.logger.info(f"Found {alert_count} alerts - using two-message mode")
                        return ("multi_message", weather, formatted_alert_text, alert_count)

            return weather

        except Exception as e:
            self.logger.error(f"Error getting weather for {location_type} {location}: {e}")
            return self.translate('commands.wx.error', error=str(e))

    async def get_weather_for_zipcode(self, zipcode: str) -> str:
        """Get weather data for a specific zipcode (legacy method)"""
        return await self.get_weather_for_location(zipcode, "zipcode")

    def zipcode_to_lat_lon(self, zipcode: str) -> tuple:
        """Convert zipcode to latitude and longitude"""
        try:
            lat, lon = geocode_zipcode_sync(self.bot, zipcode, timeout=10)
            return lat, lon
        except Exception as e:
            self.logger.error(f"Error geocoding zipcode {zipcode}: {e}")
            return None, None

    def city_to_lat_lon(self, city: str) -> tuple:
        """Convert city name to latitude and longitude using default state"""
        try:
            # Use shared geocode_city_sync function with address info
            default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            lat, lon, address_info = geocode_city_sync(
                self.bot, city, default_state=self.default_state,
                default_country=default_country,
                include_address_info=True, timeout=10
            )

            if lat and lon:
                return lat, lon, address_info or {}
            else:
                return None, None, None
        except Exception as e:
            self.logger.error(f"Error geocoding city {city}: {e}")
            return None, None, None

    def get_noaa_weather(self, lat: float, lon: float, return_periods: bool = False, max_length: int = 130, location: str = "") -> tuple:
        """Get weather forecast from NOAA and return both weather string and points data

        Args:
            lat: Latitude
            lon: Longitude
            return_periods: If True, return forecast periods array instead of formatted string
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Tuple of (weather_string_or_periods, points_data)
        """
        try:
            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            # Get weather data from NOAA
            weather_api = f"https://api.weather.gov/points/{lat_rounded},{lon_rounded}"

            # Get the forecast URL (with retry logic)
            try:
                weather_data = self.noaa_session.get(weather_api, timeout=self.url_timeout)
                if not weather_data.ok:
                    self.logger.warning(f"Error fetching weather data from NOAA: HTTP {weather_data.status_code}")
                    return self.ERROR_FETCHING_DATA, None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching weather data from NOAA: {e}")
                return self.ERROR_FETCHING_DATA, None

            weather_json = weather_data.json()
            forecast_url = weather_json['properties']['forecast']

            # Get the forecast (with retry logic)
            try:
                forecast_data = self.noaa_session.get(forecast_url, timeout=self.url_timeout)
                if not forecast_data.ok:
                    self.logger.warning(f"Error fetching weather forecast from NOAA: HTTP {forecast_data.status_code}")
                    return self.ERROR_FETCHING_DATA, None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching weather forecast from NOAA: {e}")
                return self.ERROR_FETCHING_DATA, None

            forecast_json = forecast_data.json()
            forecast = forecast_json['properties']['periods']

            # If return_periods is True, return the periods array directly
            if return_periods:
                if not forecast:
                    return self.ERROR_FETCHING_DATA, None
                return forecast, weather_json

            # Format the forecast - focus on current conditions and key info
            if not forecast:
                return "No forecast data available", weather_json

            current = forecast[0]
            temp = current.get('temperature')
            temp_unit = current.get('temperatureUnit', 'F')
            short_forecast = current.get('shortForecast', 'Unknown')
            detailed_forecast = current.get('detailedForecast', '')
            obs = self.get_observation_data(weather_json)

            # Today's high / low from the upcoming day & night periods.
            hi = lo = None
            for period in forecast:
                if period.get('isDaytime') and hi is None:
                    hi = period.get('temperature')
                elif not period.get('isDaytime') and lo is None:
                    lo = period.get('temperature')
                if hi is not None and lo is not None:
                    break

            wind_nums = re.findall(r'\d+', current.get('windSpeed', '') or '')
            wind = format_wind(current.get('windDirection', ''), max((int(n) for n in wind_nums), default=0))

            def _to_int(value):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            # "now" must be the live observation temperature. forecast[0]'s
            # temperature is the period value — the day's HIGH (or the night's LOW) —
            # which is why the current reading jumped to the daytime high once the
            # "Today" period became active. Fall back to the period temp only when no
            # nearby station reported a current temperature.
            obs_now = _to_int(obs.get('temperature'))
            if obs_now is not None:
                temp = obs_now

            rh = _to_int(obs.get('humidity'))
            dew = _to_int(obs.get('dew_point'))
            # If the obs gave one of RH/dew but not the other, derive it from the
            # temperature so the metrics line stays complete (NOAA values are °F here).
            if temp is not None and (temp_unit or 'F').upper() == 'F':
                if dew is None and rh is not None:
                    dew = dewpoint_f(temp, rh)
                elif rh is None and dew is not None:
                    rh = rh_from_dewpoint_f(temp, dew)

            weather = current_block(
                location, self.get_weather_emoji(short_forecast), short_forecast,
                _to_int(self.extract_precip_chance(detailed_forecast)), temp,
                unit=f"°{temp_unit}", feels=_to_int(obs.get('feels_like')), hi=hi, lo=lo,
                wind=wind, rh=rh, dew=dew,
                uv=_to_int(self.extract_uv_index(detailed_forecast)), budget=max_length,
            )
            return weather, weather_json

        except Exception as e:
            self.logger.error(f"Error fetching NOAA weather: {e}")
            return self.ERROR_FETCHING_DATA, None

    def get_noaa_hourly_weather(self, lat: float, lon: float) -> tuple:
        """Get hourly weather forecast from NOAA

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Tuple of (hourly_periods_list, points_data)
        """
        try:
            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            # Get weather data from NOAA
            weather_api = f"https://api.weather.gov/points/{lat_rounded},{lon_rounded}"

            # Get the forecast URL (with retry logic)
            try:
                weather_data = self.noaa_session.get(weather_api, timeout=self.url_timeout)
                if not weather_data.ok:
                    self.logger.warning(f"Error fetching weather data from NOAA: HTTP {weather_data.status_code}")
                    return self.ERROR_FETCHING_DATA, None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching weather data from NOAA: {e}")
                return self.ERROR_FETCHING_DATA, None

            weather_json = weather_data.json()
            hourly_forecast_url = weather_json['properties'].get('forecastHourly')

            if not hourly_forecast_url:
                self.logger.warning("Hourly forecast not available for this location")
                return self.ERROR_FETCHING_DATA, None

            # Get the hourly forecast (with retry logic)
            try:
                hourly_data = self.noaa_session.get(hourly_forecast_url, timeout=self.url_timeout)
                if not hourly_data.ok:
                    self.logger.warning(f"Error fetching hourly forecast from NOAA: HTTP {hourly_data.status_code}")
                    return self.ERROR_FETCHING_DATA, None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching hourly forecast from NOAA: {e}")
                return self.ERROR_FETCHING_DATA, None

            hourly_json = hourly_data.json()
            hourly_periods = hourly_json['properties']['periods']

            if not hourly_periods:
                self.logger.warning("No hourly periods returned from NOAA")
                return self.ERROR_FETCHING_DATA, None

            return hourly_periods, weather_json

        except Exception as e:
            self.logger.error(f"Error fetching NOAA hourly weather: {e}")
            return self.ERROR_FETCHING_DATA, None

    def format_hourly_forecast(self, hourly_periods: list, max_length: int = 130, location: str = "") -> str:
        """Upcoming hours as one message: header + 'time emoji temp precip' rows (style A)."""
        try:
            if not hourly_periods:
                return self.translate('commands.wx.hourly_not_available')
            now = datetime.now()
            rows = []
            for period in hourly_periods:
                st = period.get('startTime', '') or ''
                label = ""
                if st:
                    try:
                        dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
                        naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
                        if naive <= now:
                            continue
                        hr = dt.hour
                        label = "12AM" if hr == 0 else (f"{hr}AM" if hr < 12 else ("12PM" if hr == 12 else f"{hr-12}PM"))
                    except (ValueError, TypeError):
                        label = ""
                temp = period.get('temperature')
                if temp is None:
                    continue
                emoji = self.get_weather_emoji(period.get('shortForecast', ''))
                pp = period.get('probabilityOfPrecipitation', {}).get('value')
                rows.append(hourly_row(label, emoji, temp, pp))
            if not rows:
                return self.translate('commands.wx.hourly_not_available')
            header = f"{location} · hourly" if location else "hourly"
            return pack(header, rows, budget=max_length)[0]
        except Exception as e:
            self.logger.error(f"Error formatting hourly forecast: {e}")
            return self.translate('commands.wx.error_fetching')

    def format_tomorrow_forecast(self, forecast: list, max_length: int = 130, location: str = "") -> str:
        """Tomorrow's day + night periods as the 3-line tomorrow block."""
        try:
            tomorrow_day = (datetime.now() + timedelta(days=1)).strftime('%A').lower()
            today_day = datetime.now().strftime('%A').lower()
            day_p = night_p = None
            found_today = False
            for period in forecast:
                nm = (period.get('name', '') or '').lower()
                if any(w in nm for w in ['today', 'this afternoon', 'this evening', 'tonight']):
                    found_today = True
                    continue
                is_tomorrow = ('tomorrow' in nm or (tomorrow_day in nm and today_day not in nm) or found_today)
                if not is_tomorrow:
                    continue
                if period.get('isDaytime') and day_p is None:
                    day_p = period
                elif not period.get('isDaytime') and night_p is None:
                    night_p = period
                if day_p is not None and night_p is not None:
                    break
            if day_p is None and night_p is None:
                return self.translate('commands.wx.tomorrow_not_available')
            ref = day_p or night_p
            day = self._noaa_period_display_name(ref).split()[0]
            cond = ref.get('shortForecast', '')
            hi = day_p.get('temperature') if day_p else None
            lo = night_p.get('temperature') if night_p else None
            pc = self.extract_precip_chance(ref.get('detailedForecast', ''))
            precip = int(pc) if pc else None
            wnums = re.findall(r'\d+', (day_p or ref).get('windSpeed', '') or '')
            wind = format_wind((day_p or ref).get('windDirection', ''), max((int(n) for n in wnums), default=0))
            night_cond = night_p.get('shortForecast', '') if night_p else ''
            return tomorrow_block(location, day, self.get_weather_emoji(cond), cond, hi, lo, precip, wind, night_cond, budget=max_length)
        except Exception as e:
            self.logger.error(f"Error formatting tomorrow forecast: {e}")
            return self.translate('commands.wx.tomorrow_error')

    def format_multiday_forecast(self, forecast: list, num_days: int = 7, max_length: int = 130, location: str = "") -> str:
        """Multi-day summary: header + one 'Day emoji cond hi/lo' row per day (newline-joined)."""
        try:
            days = {}
            order = []
            for period in forecast:
                nml = (period.get('name', '') or '').lower()
                day = None
                for d in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'):
                    if d in nml:
                        day = d[:3].capitalize()
                        break
                if day is None:
                    if 'tomorrow' in nml:
                        day = (datetime.now() + timedelta(days=1)).strftime('%a')
                    elif 'today' in nml or 'afternoon' in nml or 'tonight' in nml or 'evening' in nml:
                        day = datetime.now().strftime('%a')
                    else:
                        continue
                if day not in days:
                    days[day] = {'hi': None, 'lo': None, 'cond': ''}
                    order.append(day)
                is_night = 'night' in nml or 'tonight' in nml or not period.get('isDaytime', True)
                temp = period.get('temperature')
                if is_night:
                    if days[day]['lo'] is None:
                        days[day]['lo'] = temp
                else:
                    if days[day]['hi'] is None:
                        days[day]['hi'] = temp
                    if not days[day]['cond']:
                        days[day]['cond'] = period.get('shortForecast', '')
            if not days:
                return self.translate('commands.wx.multiday_not_available', num_days=num_days)
            rows = []
            for day in order[:num_days]:
                info = days[day]
                cond = info['cond'] or ''
                rows.append(day_row(day, self.get_weather_emoji(cond), cond, info['hi'], info['lo']))
            header = f"{location} · {num_days}-day" if location else f"{num_days}-day"
            return "\n".join([header] + rows)
        except Exception as e:
            self.logger.error(f"Error formatting multiday forecast: {e}")
            return self.translate('commands.wx.multiday_error')

    def _add_period_details(self, period_str: str, detailed_forecast: str, current_weather_length: int, max_length: int = 130, observation_data: dict = None) -> str:
        """Add additional details (humidity, dew point, visibility, etc.) to a period string

        Args:
            period_str: The base period string (e.g., " | Today: ☀️Sunny 75°")
            detailed_forecast: The detailed forecast text to extract info from
            current_weather_length: Current length of the weather string (to check total length)
            max_length: Maximum total length allowed (default 130)
            observation_data: Optional dict with observation station data (humidity, dew_point, visibility, wind_gusts, pressure)

        Returns:
            Updated period string with additional details if space allows
        """
        result = period_str
        current_weather_length + self._count_display_width(result)

        # Extract additional details - prefer observation data if available (more accurate)
        if observation_data:
            humidity = observation_data.get('humidity')
            dew_point = observation_data.get('dew_point')
            visibility = observation_data.get('visibility')
            wind_gusts = observation_data.get('wind_gusts')
            pressure = observation_data.get('pressure')
        else:
            humidity = None
            dew_point = None
            visibility = None
            wind_gusts = None
            pressure = None

        # Fall back to parsing from detailed forecast if observation data not available
        if not humidity:
            humidity = self.extract_humidity(detailed_forecast)
        if not dew_point:
            dew_point = self.extract_dew_point(detailed_forecast)
        if not visibility:
            visibility = self.extract_visibility(detailed_forecast)
        if not wind_gusts:
            wind_gusts = self.extract_wind_gusts(detailed_forecast)
        if not pressure:
            pressure = self.extract_pressure(detailed_forecast)

        # Always try to get precip_prob from detailed forecast (not in observation data)
        precip_prob = self.extract_precip_probability(detailed_forecast)

        # Add humidity if available and space allows
        # Try to add all available details, only skip if they would exceed max_length
        if humidity:
            humidity_str = f" {humidity}%RH"
            if self._count_display_width(result + humidity_str) + current_weather_length <= max_length:
                result += humidity_str
                current_weather_length + self._count_display_width(result)

        # Add dew point if available and space allows
        if dew_point:
            dew_str = f" 💧{dew_point}°"
            if self._count_display_width(result + dew_str) + current_weather_length <= max_length:
                result += dew_str
                current_weather_length + self._count_display_width(result)

        # Add visibility if available and space allows
        if visibility:
            vis_str = f" 👁️{visibility}mi"
            if self._count_display_width(result + vis_str) + current_weather_length <= max_length:
                result += vis_str
                current_weather_length + self._count_display_width(result)

        # Add precipitation probability if available and space allows
        if precip_prob:
            precip_str = f" 🌦️{precip_prob}%"
            if self._count_display_width(result + precip_str) + current_weather_length <= max_length:
                result += precip_str
                current_weather_length + self._count_display_width(result)

        # Add wind gusts if available and space allows
        if wind_gusts:
            gust_str = f" 💨{wind_gusts}"
            if self._count_display_width(result + gust_str) + current_weather_length <= max_length:
                result += gust_str
                current_weather_length + self._count_display_width(result)

        # Add pressure if available and space allows
        if pressure:
            pressure_str = f" 📊{pressure}hPa"
            if self._count_display_width(result + pressure_str) + current_weather_length <= max_length:
                result += pressure_str

        return result

    def _count_display_width(self, text: str) -> int:
        """Count UTF-8 byte length of text. Matches RF packet byte limit from get_max_message_length()."""
        return len(text.encode('utf-8'))

    async def _send_multiday_forecast(self, message: MeshMessage, forecast_text: str):
        """Send multi-day forecast response, splitting into multiple messages if needed"""
        import asyncio

        # Get max message length dynamically
        max_length = self.get_max_message_length(message)
        # Gap between split messages so the mesh doesn't flood (tunable via config).
        delay = self.bot.config.getfloat('Bot', 'multi_message_delay_seconds', fallback=5.0)

        lines = forecast_text.split('\n')

        # Remove empty lines
        lines = [line.strip() for line in lines if line.strip()]

        if not lines:
            return

        # If single line and under max_length chars, send as-is
        if self._count_display_width(forecast_text) <= max_length:
            await self.send_response(message, forecast_text)
            return

        # Multi-line message - try to fit as many days as possible in one message
        # Only split when necessary (message would exceed max_length chars)
        current_message = ""
        message_count = 0

        for i, line in enumerate(lines):
            if not line:
                continue

            # Check if adding this line would exceed max_length characters (using display width)
            test_message = current_message + "\n" + line if current_message else line

            # Only split if message would exceed max_length chars (using display width)
            if self._count_display_width(test_message) > max_length:
                # Send current message and start new one
                if current_message:
                    # Per-user rate limit applies only to first message (trigger); skip for continuations
                    await self.send_response(
                        message, current_message,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    # Wait between messages (same as other commands)
                    if i < len(lines):
                        await asyncio.sleep(delay)

                    current_message = line
                else:
                    # Single line is too long, send it anyway (will be truncated by bot)
                    await self.send_response(
                        message, line,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    if i < len(lines) - 1:
                        await asyncio.sleep(delay)
                    current_message = ""
            else:
                # Add line to current message (fits within max_length chars)
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line

        # Send the last message if there's content (continuation; skip per-user rate limit)
        if current_message:
            await self.send_response(message, current_message, skip_user_rate_limit=True)

    def _alert_zone_for(self, lat: float, lon: float) -> str:
        """NWS county zone (e.g. 'TNC037') for a point, cached; '' on failure.

        Alerts are queried by county zone, not the exact point, because storm-based
        warning polygons often exclude the precise point even when the county is
        warned (so a point query silently returns "no alerts").
        """
        key = (lat, lon)
        if key in self._alert_zone_cache:
            return self._alert_zone_cache[key]
        zone = ""
        try:
            resp = self.noaa_session.get(
                f"https://api.weather.gov/points/{lat},{lon}", timeout=self.url_timeout
            )
            if resp.ok:
                zone = (resp.json().get('properties', {}).get('county') or '').rstrip('/').split('/')[-1]
        except Exception as e:
            self.logger.debug(f"Alert zone lookup failed for {lat},{lon} ({e}); using point query")
        self._alert_zone_cache[key] = zone
        return zone

    def get_weather_alerts_noaa(self, lat: float, lon: float, return_full_data: bool = False) -> tuple:
        """Get weather alerts from NOAA with full metadata extraction and prioritization

        Args:
            lat: Latitude
            lon: Longitude
            return_full_data: If True, return list of alert dicts instead of formatted strings

        Returns:
            If return_full_data=False: (full_first_alert_text, abbreviated_first_alert_text, alert_count)
            If return_full_data=True: (list of alert dicts, alert_count)
        """
        try:
            # Round coordinates to 4 decimal places to avoid API redirects
            lat_rounded = round(lat, 4)
            lon_rounded = round(lon, 4)

            # Query by county zone, not the exact point (storm-based warning
            # polygons often miss the precise point even when the county is warned);
            # fall back to the point query if the zone can't be resolved.
            zone = self._alert_zone_for(lat_rounded, lon_rounded)
            if zone:
                alert_url = f"https://api.weather.gov/alerts/active.atom?zone={zone}"
            else:
                alert_url = f"https://api.weather.gov/alerts/active.atom?point={lat_rounded},{lon_rounded}"

            try:
                alert_data = self.noaa_session.get(alert_url, timeout=self.url_timeout)
                if not alert_data.ok:
                    self.logger.warning(f"Error fetching weather alerts from NOAA: HTTP {alert_data.status_code}")
                    return self.ERROR_FETCHING_DATA
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching weather alerts from NOAA: {e}")
                return self.ERROR_FETCHING_DATA

            alerts = []  # Store structured alert data
            alertxml = xml.dom.minidom.parseString(alert_data.text)

            for entry in alertxml.getElementsByTagName("entry"):
                try:
                    # Extract title
                    title_elem = entry.getElementsByTagName("title")
                    title = title_elem[0].childNodes[0].nodeValue if title_elem and title_elem[0].childNodes else ""

                    # Extract summary/content for additional context (especially useful for Special Statements)
                    summary = ""
                    summary_elem = entry.getElementsByTagName("summary")
                    if summary_elem and summary_elem[0].childNodes:
                        summary = summary_elem[0].childNodes[0].nodeValue if summary_elem[0].childNodes[0].nodeValue else ""
                    # Also check for content element
                    if not summary:
                        content_elem = entry.getElementsByTagName("content")
                        if content_elem and content_elem[0].childNodes:
                            summary = content_elem[0].childNodes[0].nodeValue if content_elem[0].childNodes[0].nodeValue else ""

                    # Extract NWS headline parameter (very useful for Special Statements)
                    # Try both with and without namespace prefix
                    nws_headline = ""
                    # Try cap:parameter first
                    params = entry.getElementsByTagName("cap:parameter")
                    if not params:
                        # Try without namespace prefix
                        params = entry.getElementsByTagName("parameter")

                    for param in params:
                        value_name_elem = param.getElementsByTagName("valueName")
                        value_elem = param.getElementsByTagName("value")
                        if value_name_elem and value_elem and value_name_elem[0].childNodes and value_elem[0].childNodes:
                            value_name = value_name_elem[0].childNodes[0].nodeValue if value_name_elem[0].childNodes[0].nodeValue else ""
                            if value_name == "NWSheadline":
                                nws_headline = value_elem[0].childNodes[0].nodeValue if value_elem[0].childNodes[0].nodeValue else ""
                                break

                    # Extract CAP (Common Alerting Protocol) metadata
                    # These are in the cap namespace, so we need to search by tag name
                    event = ""
                    severity = "Unknown"
                    urgency = "Unknown"
                    certainty = "Unknown"
                    effective = ""
                    expires = ""
                    area_desc = ""
                    office = ""

                    # Parse title to extract key info (fallback if CAP data not available)
                    # Title format: "High Wind Warning issued December 16 at 3:12PM PST until December 17 at 6:00AM PST by NWS Seattle WA"
                    title_lower = title.lower()

                    # Extract event type from title
                    if "warning" in title_lower:
                        event_type = "Warning"
                        # Extract event name (e.g., "High Wind Warning" -> "High Wind")
                        event_match = re.search(r'^([^W]+?)\s+Warning', title, re.IGNORECASE)
                        if event_match:
                            event = event_match.group(1).strip()
                    elif "watch" in title_lower:
                        event_type = "Watch"
                        event_match = re.search(r'^([^W]+?)\s+Watch', title, re.IGNORECASE)
                        if event_match:
                            event = event_match.group(1).strip()
                    elif "advisory" in title_lower:
                        event_type = "Advisory"
                        event_match = re.search(r'^([^A]+?)\s+Advisory', title, re.IGNORECASE)
                        if event_match:
                            event = event_match.group(1).strip()
                    elif "statement" in title_lower:
                        event_type = "Statement"
                        # For statements, try to extract more descriptive info
                        # Pattern: "Special Weather Statement" or "Hydrologic Statement" etc.
                        event_match = re.search(r'^([^S]+?)\s+Statement', title, re.IGNORECASE)
                        event = event_match.group(1).strip() if event_match else "Special"

                        # For Special Statements, try to extract meaningful description from NWS headline or summary
                        if event.lower() in ["special", "special weather"]:
                            # First, try NWS headline (most concise and descriptive)
                            if nws_headline:
                                headline_lower = nws_headline.lower()

                                # Extract the PRIMARY topic - look for the main subject/action
                                # Strategy: Find the most important noun/topic, prioritizing specific threats
                                # Order matters - check more specific threats first

                                # Very specific threats (highest priority)
                                if any(phrase in headline_lower for phrase in ['debris flow', 'mudslide']):
                                    event = "Debris Flow"
                                elif 'landslide' in headline_lower:
                                    # Check if there's a more specific context
                                    if 'burn' in headline_lower or 'burned area' in headline_lower:
                                        event = "Landslide (Burn)"
                                    else:
                                        event = "Landslide"
                                # Weather phenomena
                                elif any(phrase in headline_lower for phrase in ['flash flood', 'river flood']) or 'flood' in headline_lower or 'flooding' in headline_lower:
                                    event = "Flood"
                                elif any(phrase in headline_lower for phrase in ['high wind', 'strong wind', 'damaging wind']) or 'wind' in headline_lower or 'gust' in headline_lower:
                                    event = "Wind"
                                elif any(phrase in headline_lower for phrase in ['heavy rain', 'excessive rain']):
                                    event = "Heavy Rain"
                                elif 'rain' in headline_lower or 'rainfall' in headline_lower or 'precipitation' in headline_lower:
                                    # If rain is mentioned with another threat, prioritize the other threat
                                    # But if rain is the main topic, use it
                                    if not any(word in headline_lower for word in ['landslide', 'flood', 'wind', 'snow']):
                                        event = "Rainfall"
                                    # Otherwise, the other threat was already caught above
                                elif any(phrase in headline_lower for phrase in ['heavy snow', 'blizzard', 'winter storm']) or 'snow' in headline_lower or 'winter' in headline_lower:
                                    event = "Snow"
                                elif any(phrase in headline_lower for phrase in ['dense fog', 'low visibility']):
                                    event = "Fog"
                                elif 'fog' in headline_lower or 'visibility' in headline_lower:
                                    event = "Visibility"
                                elif any(phrase in headline_lower for phrase in ['extreme heat', 'excessive heat']):
                                    event = "Heat"
                                elif 'heat' in headline_lower or 'temperature' in headline_lower:
                                    event = "Temperature"
                                elif any(phrase in headline_lower for phrase in ['storm surge', 'coastal flood']) or 'marine' in headline_lower or 'coastal' in headline_lower:
                                    event = "Marine"
                                else:
                                    # Try to extract first meaningful word/phrase from headline
                                    # Remove common words and extract key terms
                                    headline_words = headline_lower.split()
                                    # Skip common words
                                    skip_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'will', 'lead', 'increased', 'threat', 'remains', 'effect', 'until', 'during', 'last', 'week', 'including', 'today'}
                                    meaningful_words = [w for w in headline_words if w not in skip_words and len(w) > 3]
                                    if meaningful_words:
                                        # Take first meaningful word, capitalize it
                                        event = meaningful_words[0].capitalize()

                            # If still generic, try summary
                            if event.lower() in ["special", "special weather"] and summary:
                                summary_lower = summary.lower()
                                # Look for key phrases in summary that indicate statement type
                                if any(word in summary_lower for word in ['landslide', 'debris flow', 'mudslide']):
                                    event = "Landslide"
                                elif any(word in summary_lower for word in ['hydrologic', 'river', 'flood', 'stream']):
                                    event = "Hydrologic"
                                elif any(word in summary_lower for word in ['marine', 'coastal', 'beach', 'surf']):
                                    event = "Marine"
                                elif any(word in summary_lower for word in ['avalanche', 'snow', 'mountain']):
                                    event = "Avalanche"
                                elif any(word in summary_lower for word in ['air quality', 'smoke', 'pollution']):
                                    event = "Air Quality"
                                elif any(word in summary_lower for word in ['wind', 'gust']):
                                    event = "Wind"
                                elif any(word in summary_lower for word in ['rain', 'precipitation', 'shower', 'rainfall']):
                                    event = "Rainfall"
                                elif any(word in summary_lower for word in ['temperature', 'heat', 'cold', 'freeze']):
                                    event = "Temperature"
                                elif any(word in summary_lower for word in ['visibility', 'fog', 'haze']):
                                    event = "Visibility"

                            # If still generic, check if title has "Weather" in it
                            if event.lower() in ["special", "special weather"]:
                                event = "Weather" if "weather" in title_lower else "Special"
                    else:
                        event_type = "Unknown"
                        event = title.split()[0] if title else ""

                    # Extract times from title
                    # Pattern: "issued December 16 at 3:12PM PST until December 17 at 6:00AM PST"
                    issued_match = re.search(r'issued\s+([^u]+?)\s+until\s+(.+?)\s+by', title, re.IGNORECASE)
                    if issued_match:
                        effective = issued_match.group(1).strip()
                        expires = issued_match.group(2).strip()
                    else:
                        # Try alternative patterns
                        until_match = re.search(r'until\s+(.+?)\s+by', title, re.IGNORECASE)
                        if until_match:
                            expires = until_match.group(1).strip()

                    # Extract office from title
                    # Pattern: "by NWS Seattle WA"
                    office_match = re.search(r'by\s+(.+?)$', title, re.IGNORECASE)
                    if office_match:
                        office = office_match.group(1).strip()

                    # Try to extract CAP elements if available (they may be in different namespaces)
                    # Look for cap:event, cap:severity, etc. in the XML
                    # CAP elements might be in namespace like "cap:event" or just "event" in a cap namespace
                    def get_node_value(node):
                        """Extract text value from XML node"""
                        if not node or not node.childNodes:
                            return ""
                        # Get all text nodes
                        text_parts = []
                        for child in node.childNodes:
                            if child.nodeType == child.TEXT_NODE or hasattr(child, 'nodeValue') and child.nodeValue:
                                text_parts.append(child.nodeValue)
                        return " ".join(text_parts).strip()

                    # Search for CAP elements by tag name (handles namespaces)
                    for child in entry.childNodes:
                        if hasattr(child, 'tagName'):
                            tag_name = child.tagName
                            tag_lower = tag_name.lower()

                            # Handle both "cap:event" and "event" formats
                            if ('event' in tag_lower or tag_name.endswith(':event')) and not event:
                                event_val = get_node_value(child)
                                if event_val:
                                    event = event_val
                            elif 'severity' in tag_lower or tag_name.endswith(':severity'):
                                severity_val = get_node_value(child)
                                if severity_val:
                                    severity = severity_val
                            elif 'urgency' in tag_lower or tag_name.endswith(':urgency'):
                                urgency_val = get_node_value(child)
                                if urgency_val:
                                    urgency = urgency_val
                            elif 'certainty' in tag_lower or tag_name.endswith(':certainty'):
                                certainty_val = get_node_value(child)
                                if certainty_val:
                                    certainty = certainty_val
                            elif 'effective' in tag_lower or tag_name.endswith(':effective'):
                                effective_val = get_node_value(child)
                                if effective_val:
                                    effective = effective_val
                            elif 'expires' in tag_lower or tag_name.endswith(':expires'):
                                expires_val = get_node_value(child)
                                if expires_val:
                                    expires = expires_val
                            elif ('areadesc' in tag_lower or 'area' in tag_lower or
                                  tag_name.endswith(':areadesc') or tag_name.endswith(':area')):
                                area_val = get_node_value(child)
                                if area_val:
                                    area_desc = area_val

                    # Also try searching by namespace-aware methods
                    # Some XML parsers handle namespaces differently
                    try:
                        # Try to get elements by local name (ignoring namespace prefix)
                        for node in entry.getElementsByTagName("*"):
                            if hasattr(node, 'localName'):
                                local_name = node.localName.lower()
                                node_val = get_node_value(node)
                                if node_val:
                                    if local_name == 'event' and not event:
                                        event = node_val
                                    elif local_name == 'severity' and severity == "Unknown":
                                        severity = node_val
                                    elif local_name == 'urgency' and urgency == "Unknown":
                                        urgency = node_val
                                    elif local_name == 'certainty' and certainty == "Unknown":
                                        certainty = node_val
                                    elif local_name == 'effective' and not effective:
                                        effective = node_val
                                    elif local_name == 'expires' and not expires:
                                        expires = node_val
                                    elif local_name in ['areadesc', 'area'] and not area_desc:
                                        area_desc = node_val
                    except:
                        pass  # Namespace-aware methods may not be available

                    # Infer severity from event type if not found
                    if severity == "Unknown":
                        if any(word in event.lower() for word in ['extreme', 'tornado', 'hurricane', 'blizzard']):
                            severity = "Extreme"
                        elif any(word in event.lower() for word in ['severe', 'warning']):
                            severity = "Severe"
                        elif any(word in event.lower() for word in ['advisory', 'moderate']):
                            severity = "Moderate"
                        else:
                            severity = "Minor"

                    # Infer urgency from event type if not found
                    if urgency == "Unknown":
                        if event_type == "Warning":
                            urgency = "Immediate"
                        elif event_type == "Watch":
                            urgency = "Expected"
                        else:
                            urgency = "Future"

                    # Calculate expiration time for prioritization
                    if expires:
                        try:
                            # Try to parse expiration time
                            # Format might be "December 17 at 6:00AM PST" or ISO format
                            if 'at' in expires.lower():
                                # Parse "December 17 at 6:00AM PST"
                                from datetime import datetime
                                datetime.now()
                                # Extract date and time parts
                                date_match = re.search(r'(\w+\s+\d+)', expires)
                                time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', expires, re.IGNORECASE)
                                if date_match and time_match:
                                    # For simplicity, assume it's within next 7 days
                                    pass  # Default estimate
                        except:
                            pass

                    alert_dict = {
                        'title': title,
                        'summary': summary,  # Store summary for potential use in formatting
                        'nws_headline': nws_headline,  # Store NWS headline for Special Statements
                        'event': event,
                        'event_type': event_type,
                        'severity': severity,
                        'urgency': urgency,
                        'certainty': certainty,
                        'effective': effective,
                        'expires': expires,
                        'area_desc': area_desc,
                        'office': office
                    }

                    alerts.append(alert_dict)

                except Exception as e:
                    self.logger.warning(f"Error parsing alert entry: {e}")
                    # Fallback: just use title
                    if title:
                        alerts.append({
                            'title': title,
                            'summary': '',
                            'nws_headline': '',
                            'event': title.split()[0] if title else "",
                            'event_type': 'Unknown',
                            'severity': 'Unknown',
                            'urgency': 'Unknown',
                            'certainty': 'Unknown',
                            'effective': '',
                            'expires': '',
                            'area_desc': '',
                            'office': ''
                        })

            if not alerts:
                return self.NO_ALERTS

            # Post-process alerts to differentiate duplicate Special Statements
            # If multiple statements have the same event, add distinguishing details
            alerts = self._differentiate_duplicate_statements(alerts)

            # Prioritize alerts using hybrid scoring
            alerts = self._prioritize_alerts(alerts)

            if return_full_data:
                return alerts, len(alerts)

            # Format for compact display (backward compatibility)
            # Return first alert formatted, plus count
            first_alert = alerts[0]
            full_first_alert_text = self._format_alert_compact(first_alert, include_details=True)
            abbreviated_first_alert_text = self._format_alert_compact(first_alert, include_details=False)

            return full_first_alert_text, abbreviated_first_alert_text, len(alerts)

        except Exception as e:
            self.logger.error(f"Error fetching NOAA weather alerts: {e}")
            return self.ERROR_FETCHING_DATA


    def _differentiate_duplicate_statements(self, alerts: list) -> list:
        """Differentiate Special Statements that have the same event type by adding unique details

        Args:
            alerts: List of alert dicts

        Returns:
            List of alerts with differentiated event names for duplicate statements
        """
        # Group alerts by event type and event name
        statement_groups = {}
        for alert in alerts:
            if alert.get('event_type') == 'Statement':
                event = alert.get('event', 'Special')
                if event not in statement_groups:
                    statement_groups[event] = []
                statement_groups[event].append(alert)

        # For each group with multiple statements, differentiate them
        for event, group in statement_groups.items():
            if len(group) > 1:
                # Multiple statements with same event - need to differentiate
                for i, alert in enumerate(group):
                    nws_headline = alert.get('nws_headline', '')
                    summary = alert.get('summary', '')
                    effective = alert.get('effective', '')
                    alert.get('expires', '')

                    # Try to extract unique distinguishing details
                    distinguishing_detail = ""

                    # Strategy 1: Extract unique keywords from headline that aren't in other headlines
                    if nws_headline:
                        headline_lower = nws_headline.lower()
                        # Look for unique time references
                        if 'today' in headline_lower or 'now' in headline_lower:
                            distinguishing_detail = " (Today)"
                        elif 'week' in headline_lower or 'past week' in headline_lower:
                            distinguishing_detail = " (Week)"
                        elif 'continues' in headline_lower or 'remains' in headline_lower:
                            distinguishing_detail = " (Ongoing)"

                        # Look for unique severity/impact words
                        if not distinguishing_detail:
                            if 'increased' in headline_lower or 'increasing' in headline_lower:
                                distinguishing_detail = " (Increased)"
                            elif 'new' in headline_lower:
                                distinguishing_detail = " (New)"
                            elif 'update' in headline_lower:
                                distinguishing_detail = " (Update)"

                    # Strategy 2: Use timing to differentiate (morning vs afternoon vs evening)
                    if not distinguishing_detail and effective:
                        try:
                            from datetime import datetime
                            # Try to parse effective time
                            if 'T' in effective:
                                dt = datetime.fromisoformat(effective.replace('Z', '+00:00'))
                                hour = dt.hour
                                if 5 <= hour < 12:
                                    distinguishing_detail = " (AM)"
                                elif 12 <= hour < 17:
                                    distinguishing_detail = " (PM)"
                                elif 17 <= hour < 21:
                                    distinguishing_detail = " (Eve)"
                                else:
                                    distinguishing_detail = " (Night)"
                        except:
                            pass

                    # Strategy 3: Extract unique topic from summary if headline didn't help
                    if not distinguishing_detail and summary:
                        summary_lower = summary.lower()
                        # Look for secondary topics that might be unique
                        # Check for specific locations, conditions, or impacts
                        if 'burn' in summary_lower or 'burned area' in summary_lower:
                            distinguishing_detail = " (Burn)"
                        elif 'coastal' in summary_lower:
                            distinguishing_detail = " (Coastal)"
                        elif 'urban' in summary_lower:
                            distinguishing_detail = " (Urban)"
                        elif 'mountain' in summary_lower or 'cascade' in summary_lower:
                            distinguishing_detail = " (Mtn)"

                    # Strategy 4: Use index as last resort (but make it subtle)
                    if not distinguishing_detail:
                        distinguishing_detail = f" ({i+1})"

                    # Update the event name with distinguishing detail
                    alert['event'] = event + distinguishing_detail

        return alerts

    def _prioritize_alerts(self, alerts: list) -> list:
        """Prioritize alerts using hybrid scoring system

        Scoring:
        - Severity: Extreme=100, Severe=75, Moderate=50, Minor=25, Unknown=0
        - Urgency: Immediate=50, Expected=30, Future=10, Past=0
        - Event Type: Warning=40, Watch=30, Advisory=20, Statement=10
        - Time: (hours until expiration) * -5 (sooner = higher score)

        Returns sorted list (highest priority first)
        """
        def calculate_score(alert):
            score = 0

            # Severity score
            severity_scores = {
                'Extreme': 100,
                'Severe': 75,
                'Moderate': 50,
                'Minor': 25,
                'Unknown': 0
            }
            score += severity_scores.get(alert.get('severity', 'Unknown'), 0)

            # Urgency score
            urgency_scores = {
                'Immediate': 50,
                'Expected': 30,
                'Future': 10,
                'Past': 0,
                'Unknown': 0
            }
            score += urgency_scores.get(alert.get('urgency', 'Unknown'), 0)

            # Event type score
            event_type_scores = {
                'Warning': 40,
                'Watch': 30,
                'Advisory': 20,
                'Statement': 10,
                'Unknown': 0
            }
            score += event_type_scores.get(alert.get('event_type', 'Unknown'), 0)

            # Time urgency (estimate hours until expiration)
            expires = alert.get('expires', '')
            expires_hours = 999  # Default to far future
            if expires:
                try:
                    # Try to parse expiration time
                    if 'at' in expires.lower():
                        # Rough estimate: if it says "6:00AM" assume it's today or tomorrow
                        time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', expires, re.IGNORECASE)
                        if time_match:
                            # For simplicity, assume alerts expire within 48 hours
                            expires_hours = 24  # Default estimate
                except:
                    pass

            # Time score: sooner expiration = higher priority
            # Subtract hours (sooner = higher score)
            score += max(0, 50 - expires_hours)

            return score

        # Sort by score (descending), then by event type, then by title
        sorted_alerts = sorted(alerts, key=lambda a: (
            -calculate_score(a),  # Negative for descending
            {'Warning': 0, 'Watch': 1, 'Advisory': 2, 'Statement': 3, 'Unknown': 4}.get(a.get('event_type', 'Unknown'), 4),
            a.get('title', '')
        ))

        return sorted_alerts

    def _format_alert_compact(self, alert: dict, include_details: bool = True) -> str:
        """Format a single alert compactly

        Args:
            alert: Alert dict with event, event_type, severity, expires, office, etc.
            include_details: If True, include expiration time and office

        Returns:
            Formatted alert string
        """
        event = alert.get('event', '')
        event_type = alert.get('event_type', '')
        severity = alert.get('severity', 'Unknown')
        expires = alert.get('expires', '')
        office = alert.get('office', '')

        # Get severity emoji
        severity_emoji = {
            'Extreme': '🔴',
            'Severe': '🟠',
            'Moderate': '🟡',
            'Minor': '⚪',
            'Unknown': '⚪'
        }.get(severity, '⚪')

        # Get event type emoji/indicator
        {
            'Warning': '⚠️',
            'Watch': '👁️',
            'Advisory': 'ℹ️',
            'Statement': '📢'
        }.get(event_type, '')

        # Format event type abbreviation
        event_type_abbrev = {
            'Warning': 'Warn',
            'Watch': 'Watch',
            'Advisory': 'Adv',
            'Statement': 'Stmt'
        }.get(event_type, event_type)

        # Build compact alert string
        if include_details:
            # Full format: "🟠High Wind Warn til 6AM by NWS SEA"
            # Start with emoji directly concatenated to text (no space)
            result = severity_emoji

            # Add event and type
            if event:
                # Check if event already contains the event type to avoid duplication
                event_lower = event.lower()
                event_type_lower = event_type.lower()
                if event_type_lower in event_lower:
                    # Event already contains type (e.g., "High Wind Warning"), just use event
                    event_short = event
                    if len(event) > 15:
                        # Take first words
                        words = event.split()
                        event_short = ' '.join(words[:2]) if len(words) > 2 else event[:15]
                    result += event_short
                else:
                    # Event doesn't contain type, add it
                    event_short = event
                    if len(event) > 15:
                        # Take first words
                        words = event.split()
                        event_short = ' '.join(words[:2]) if len(words) > 2 else event[:15]
                    result += f"{event_short} {event_type_abbrev}"
            else:
                result += event_type_abbrev

            # Add expiration time if available
            if expires:
                expires_compact = self.compact_time(expires)
                # Extract just the time part
                # "Dec 17 1AM" -> "til 1AM" (prefer just time for compactness)
                # Check if it's in compact format with month name (from ISO parsing)
                if any(month in expires_compact for month in ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]):
                    # Has date, extract just time part for compactness
                    time_match = re.search(r'(\d+)(AM|PM)', expires_compact, re.IGNORECASE)
                    if time_match:
                        hour = time_match.group(1)
                        am_pm = time_match.group(2)
                        expires_short = f" til {hour}{am_pm}"
                    else:
                        # Fallback: use compact version but limit length
                        expires_short = f" til {expires_compact[:15]}"
                else:
                    # Try to extract time pattern from other formats
                    time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', expires_compact, re.IGNORECASE)
                    if time_match:
                        hour = time_match.group(1)
                        am_pm = time_match.group(3)
                        expires_short = f" til {hour}{am_pm}"
                    else:
                        # If no time pattern found, use compact version (truncated)
                        expires_short = f" til {expires_compact[:15]}"
                result += expires_short

            # Add office if available (abbreviate city name)
            if office:
                # Extract city from office (e.g., "NWS Seattle WA" -> "NWS SEA")
                office_parts = office.split()
                if len(office_parts) >= 2:
                    # Assume format: "NWS Seattle WA" or "NWS Seattle"
                    office_org = office_parts[0]  # "NWS"
                    city = office_parts[1] if len(office_parts) > 1 else ""
                    city_abbrev = self.abbreviate_city_name(city)
                    office_short = f" by {office_org} {city_abbrev}"
                else:
                    office_short = f" by {office[:10]}"  # Truncate
                result += office_short

            return result
        else:
            # Abbreviated format: just event type and severity
            return f"{severity_emoji}{event} {event_type_abbrev}" if event else f"{severity_emoji}{event_type_abbrev}"

    def _format_alerts_compact_summary(self, alerts: list, alert_count: int, max_length: int = 130) -> str:
        """Format multiple alerts with prioritized first alert and summary of others

        Args:
            alerts: List of prioritized alert dicts
            alert_count: Total number of alerts
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Compact formatted string: "4 alerts: 🟠High Wind Warn til 6AM | +3: 🌊Flood Watch, ❄️Freeze Adv, 🌫️Dense Fog Adv"
        """
        if not alerts:
            return f"{alert_count} alerts"

        # Format first (highest priority) alert with details
        first_alert = alerts[0]
        first_alert_text = self._format_alert_compact(first_alert, include_details=True)

        # If only one alert, return it
        if alert_count == 1:
            return f"{alert_count} alert: {first_alert_text}"

        # Build summary of remaining alerts
        remaining_alerts = alerts[1:]
        remaining_count = len(remaining_alerts)

        # Format remaining alerts as event types only
        remaining_parts = []
        for alert in remaining_alerts[:5]:  # Limit to 5 to avoid overflow
            event = alert.get('event', '')
            event_type = alert.get('event_type', '')

            # Get event type abbreviation
            event_type_abbrev = {
                'Warning': 'Warn',
                'Watch': 'Watch',
                'Advisory': 'Adv',
                'Statement': 'Stmt'
            }.get(event_type, event_type)

            # Get emoji for event type
            event_emoji = self._get_event_emoji(event, event_type)

            # Build compact event string
            if event:
                # Abbreviate long event names
                event_short = event
                if len(event) > 12:
                    words = event.split()
                    if len(words) > 1:
                        event_short = words[0]  # Just first word
                    else:
                        event_short = event[:12]
                remaining_parts.append(f"{event_emoji}{event_short} {event_type_abbrev}")
            else:
                remaining_parts.append(f"{event_emoji}{event_type_abbrev}")

        # Build summary
        if remaining_count > 5:
            remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:5])}..."
        else:
            remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts)}"

        # Combine: first alert + summary
        result = f"{alert_count} alerts: {first_alert_text} | {remaining_summary}"

        # Check if it fits in max_length chars, truncate if needed
        if self._count_display_width(result) > max_length:
            # Try shorter first alert
            first_alert_text_short = self._format_alert_compact(first_alert, include_details=False)
            result = f"{alert_count} alerts: {first_alert_text_short} | {remaining_summary}"

            # If still too long, truncate remaining summary
            if self._count_display_width(result) > max_length:
                max_remaining = 3
                while max_remaining > 0 and self._count_display_width(result) > max_length:
                    if remaining_count > max_remaining:
                        remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:max_remaining])}..."
                    else:
                        remaining_summary = f"+{remaining_count}: {', '.join(remaining_parts[:max_remaining])}"
                    result = f"{alert_count} alerts: {first_alert_text_short} | {remaining_summary}"
                    max_remaining -= 1

        return result

    def _get_event_emoji(self, event: str, event_type: str) -> str:
        """Get emoji for event type"""
        event_lower = event.lower() if event else ""

        # Weather event emojis
        if any(word in event_lower for word in ['flood', 'flooding']):
            return '🌊'
        elif any(word in event_lower for word in ['wind', 'gale']):
            return '💨'
        elif any(word in event_lower for word in ['snow', 'winter', 'blizzard']):
            return '❄️'
        elif any(word in event_lower for word in ['fog', 'smoke', 'haze']):
            return '🌫️'
        elif any(word in event_lower for word in ['heat', 'excessive heat']):
            return '🌡️'
        elif any(word in event_lower for word in ['freeze', 'frost']):
            return '🧊'
        elif any(word in event_lower for word in ['thunderstorm', 'tornado']):
            return '⛈️'
        elif any(word in event_lower for word in ['fire', 'red flag']):
            return '🔥'
        elif any(word in event_lower for word in ['hurricane', 'tropical']):
            return '🌀'
        elif any(word in event_lower for word in ['tsunami']):
            return '🌊'
        else:
            # Default by event type
            return {
                'Warning': '⚠️',
                'Watch': '👁️',
                'Advisory': 'ℹ️',
                'Statement': '📢'
            }.get(event_type, '⚠️')

    def _format_alert_full(self, alert: dict, index: int = None) -> str:
        """Format a single alert with full details for multi-message display

        Args:
            alert: Alert dict
            index: Optional alert number (1-based)

        Returns:
            Formatted alert string with start/stop times
        """
        event = alert.get('event', '')
        event_type = alert.get('event_type', '')
        severity = alert.get('severity', 'Unknown')
        effective = alert.get('effective', '')
        expires = alert.get('expires', '')
        office = alert.get('office', '')

        # Get severity emoji
        severity_emoji = {
            'Extreme': '🔴',
            'Severe': '🟠',
            'Moderate': '🟡',
            'Minor': '⚪',
            'Unknown': '⚪'
        }.get(severity, '⚪')

        # Format event type
        event_type_abbrev = {
            'Warning': 'Warn',
            'Watch': 'Watch',
            'Advisory': 'Adv',
            'Statement': 'Stmt'
        }.get(event_type, event_type)

        # Build parts
        parts = []

        # Add index if provided
        if index is not None:
            parts.append(f"{index}.")

        # Add severity emoji and event
        if event:
            # Check if event already contains the event type to avoid duplication
            event_lower = event.lower()
            event_type_lower = event_type.lower()
            if event_type_lower in event_lower:
                # Event already contains type (e.g., "High Wind Warning"), just use event
                parts.append(f"{severity_emoji}{event}")
            else:
                # Event doesn't contain type, add it
                parts.append(f"{severity_emoji}{event} {event_type_abbrev}")
        else:
            parts.append(f"{severity_emoji}{event_type_abbrev}")

        # Add times
        time_parts = []
        if effective:
            effective_compact = self.compact_time(effective)
            # Extract just the essential time info
            # Try pattern: "December 16 at 3:12PM" or "Dec 16 3:12PM"
            time_match = re.search(r'(\w+\s+\d+)\s+(?:at\s+)?(\d+):?(\d+)?(AM|PM)', effective_compact, re.IGNORECASE)
            if time_match:
                date_part = time_match.group(1)
                hour = time_match.group(2)
                am_pm = time_match.group(4)
                time_parts.append(f"from {date_part} {hour}{am_pm}")
            else:
                # Fallback: just use compacted version, truncate if needed
                effective_short = effective_compact[:25]
                time_parts.append(f"from {effective_short}")

        if expires:
            expires_compact = self.compact_time(expires)
            # Extract time part
            # Try pattern: "December 17 at 6:00AM" or "Dec 17 6AM"
            time_match = re.search(r'(\w+\s+\d+)\s+(?:at\s+)?(\d+):?(\d+)?(AM|PM)', expires_compact, re.IGNORECASE)
            if time_match:
                date_part = time_match.group(1)
                hour = time_match.group(2)
                am_pm = time_match.group(4)
                time_parts.append(f"til {date_part} {hour}{am_pm}")
            else:
                # Fallback: just use compacted version, truncate if needed
                expires_short = expires_compact[:25]
                time_parts.append(f"til {expires_short}")

        if time_parts:
            parts.append(" ".join(time_parts))

        # Add office (abbreviated)
        if office:
            office_parts = office.split()
            if len(office_parts) >= 2:
                office_org = office_parts[0]
                city = office_parts[1]
                city_abbrev = self.abbreviate_city_name(city)
                parts.append(f"by {office_org} {city_abbrev}")
            else:
                parts.append(f"by {office[:15]}")

        return " ".join(parts)

    async def _send_full_alert_list(self, message: MeshMessage, lat: float, lon: float):
        """Send full list of alerts with details, splitting across multiple messages if needed"""
        import asyncio

        # Get full alert data
        alerts_result = self.get_weather_alerts_noaa(lat, lon, return_full_data=True)
        if alerts_result == self.ERROR_FETCHING_DATA:
            await self.send_response(message, self.translate('commands.wx.error_fetching'))
            return
        elif alerts_result == self.NO_ALERTS:
            await self.send_response(message, "No weather alerts")
            return

        alerts, alert_count = alerts_result

        if not alerts:
            await self.send_response(message, "No weather alerts")
            return

        # Format each alert with full details
        alert_lines = []
        for i, alert in enumerate(alerts, 1):
            alert_line = self._format_alert_full(alert, index=i)
            alert_lines.append(alert_line)

        # Send alerts, splitting into multiple messages if needed (gap tunable via config).
        sleep_time = self.bot.config.getfloat('Bot', 'multi_message_delay_seconds', fallback=5.0)

        # Get max message length dynamically
        max_length = self.get_max_message_length(message)

        # Group alerts into messages that fit within max_length chars
        current_message = f"{alert_count} alerts:"
        messages = []

        for line in alert_lines:
            # Check if adding this line would exceed limit
            test_message = current_message + "\n" + line if current_message else line
            if self._count_display_width(test_message) > max_length:
                # Current message is full, start new one
                if current_message:
                    messages.append(current_message)
                current_message = line
            else:
                # Add to current message
                if current_message:
                    current_message += "\n" + line
                else:
                    current_message = line

        # Add last message
        if current_message:
            messages.append(current_message)

        # Send all messages (per-user rate limit applies only to first; skip for continuations)
        for i, msg in enumerate(messages):
            await self.send_response(message, msg, skip_user_rate_limit=(i > 0))
            if i < len(messages) - 1:
                await asyncio.sleep(sleep_time)

    def abbreviate_alert_title(self, title: str) -> str:
        """Abbreviate alert title for brevity"""
        # Common alert type abbreviations
        replacements = {
            "warning": "Warn",
            "watch": "Watch",
            "advisory": "Adv",
            "statement": "Stmt",
            "severe thunderstorm": "SvrT-Storm",
            "tornado": "Tornado",
            "flash flood": "FlashFlood",
            "flood": "Flood",
            "winter storm": "WinterStorm",
            "blizzard": "Blizzard",
            "ice storm": "IceStorm",
            "freeze": "Freeze",
            "frost": "Frost",
            "heat": "Heat",
            "excessive heat": "ExHeat",
            "extreme heat": "ExtHeat",
            "wind": "Wind",
            "high wind": "HighWind",
            "wind advisory": "WindAdv",
            "fire weather": "FireWx",
            "red flag": "RedFlag",
            "dense fog": "DenseFog",
            "issued": "iss",
            "until": "til",
            "effective": "eff",
            "expires": "exp",
            "dense smoke": "DenseSmoke",
            "air quality": "AirQuality",
            "coastal flood": "CoastalFlood",
            "lakeshore flood": "LakeshoreFlood",
            "rip current": "RipCurrent",
            "high surf": "HighSurf",
            "hurricane": "Hurricane",
            "tropical storm": "TropStorm",
            "tropical depression": "TropDep",
            "storm surge": "StormSurge",
            "tsunami": "Tsunami",
            "earthquake": "Earthquake",
            "volcano": "Volcano",
            "avalanche": "Avalanche",
            "landslide": "Landslide",
            "debris flow": "DebrisFlow",
            "dust storm": "DustStorm",
            "sandstorm": "Sandstorm",
            "blowing dust": "BlwDust",
            "blowing sand": "BlwSand"
        }

        result = title
        for key, value in replacements.items():
            # Case insensitive replace
            result = result.replace(key, value).replace(key.capitalize(), value).replace(key.upper(), value)

        # Limit to reasonable length
        if len(result) > 30:
            result = result[:27] + "..."

        return result

    def abbreviate_city_name(self, city: str) -> str:
        """Abbreviate city names for compact display (e.g., Seattle -> SEA)"""
        if not city:
            return city

        # Common city abbreviations
        city_abbrevs = {
            "Seattle": "SEA",
            "Portland": "PDX",
            "San Francisco": "SF",
            "Los Angeles": "LA",
            "New York": "NYC",
            "Chicago": "CHI",
            "Houston": "HOU",
            "Phoenix": "PHX",
            "Philadelphia": "PHL",
            "San Antonio": "SAT",
            "San Diego": "SAN",
            "Dallas": "DAL",
            "San Jose": "SJC",
            "Austin": "AUS",
            "Jacksonville": "JAX",
            "Columbus": "CMH",
            "Fort Worth": "FTW",
            "Charlotte": "CLT",
            "Denver": "DEN",
            "Washington": "DC",
            "Boston": "BOS",
            "El Paso": "ELP",
            "Detroit": "DTW",
            "Nashville": "BNA",
            "Oklahoma City": "OKC",
            "Las Vegas": "LAS",
            "Memphis": "MEM",
            "Louisville": "SDF",
            "Baltimore": "BWI",
            "Milwaukee": "MKE",
            "Albuquerque": "ABQ",
            "Tucson": "TUS",
            "Fresno": "FAT",
            "Sacramento": "SAC",
            "Kansas City": "KC",
            "Mesa": "MSC",
            "Atlanta": "ATL",
            "Omaha": "OMA",
            "Colorado Springs": "COS",
            "Raleigh": "RDU",
            "Virginia Beach": "ORF",
            "Miami": "MIA",
            "Oakland": "OAK",
            "Minneapolis": "MSP",
            "Tulsa": "TUL",
            "Cleveland": "CLE",
            "Wichita": "ICT",
            "Arlington": "ARL",
            "Tampa": "TPA",
            "New Orleans": "MSY",
            "Honolulu": "HNL",
            "Anchorage": "ANC",
            "Bellingham": "BLI",
            "Everett": "EVE",
            "Spokane": "GEG",
            "Tacoma": "TAC",
            "Yakima": "YKM",
            "Olympia": "OLM",
            "Vancouver": "YVR",
            "Victoria": "YYJ"
        }

        # Check for exact match first
        if city in city_abbrevs:
            return city_abbrevs[city]

        # Check for partial matches (e.g., "Seattle WA" -> "SEA")
        for full_name, abbrev in city_abbrevs.items():
            if full_name in city:
                return abbrev

        # If no match, try to create abbreviation from first letters of words
        words = city.split()
        if len(words) > 1:
            # Take first letter of each word, up to 3-4 letters
            abbrev = ''.join([w[0].upper() for w in words[:3]])
            if len(abbrev) <= 4:
                return abbrev

        # Fallback: return first 3-4 uppercase letters
        return city[:4].upper() if len(city) >= 4 else city.upper()

    def compact_time(self, time_str: str) -> str:
        """Compact time format: '6:00AM' -> '6AM', 'December 16 at 3:12PM' -> 'Dec 16 3:12PM'
        Also handles ISO format: '2025-12-17T01:00:00-08:00' -> 'Dec 17 1AM'"""
        if not time_str:
            return time_str

        # Check if it's ISO format (contains 'T' and looks like datetime)
        if 'T' in time_str and re.match(r'\d{4}-\d{2}-\d{2}T', time_str):
            try:
                from datetime import datetime
                # Parse ISO format
                # Handle various ISO formats: 2025-12-17T01:00:00-08:00, 2025-12-17T01:0, etc.
                # Try to parse with timezone info first
                try:
                    dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                except:
                    # Try without timezone
                    dt_str = time_str.split('T')[0] + 'T' + time_str.split('T')[1].split('-')[0].split('+')[0]
                    dt = datetime.fromisoformat(dt_str)

                # Format as "Dec 17 1AM"
                month_abbrevs = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                month = month_abbrevs[dt.month - 1]
                day = dt.day
                hour = dt.hour

                # Convert to 12-hour format
                if hour == 0:
                    hour_12 = 12
                    am_pm = "AM"
                elif hour < 12:
                    hour_12 = hour
                    am_pm = "AM"
                elif hour == 12:
                    hour_12 = 12
                    am_pm = "PM"
                else:
                    hour_12 = hour - 12
                    am_pm = "PM"

                return f"{month} {day} {hour_12}{am_pm}"
            except Exception:
                # If parsing fails, fall through to regular processing
                pass

        # Remove leading zeros from hours: "6:00AM" -> "6AM", "10:00PM" -> "10PM"
        time_str = re.sub(r'(\d+):00(AM|PM)', r'\1\2', time_str)

        # Abbreviate month names
        month_abbrevs = {
            "January": "Jan", "February": "Feb", "March": "Mar", "April": "Apr",
            "May": "May", "June": "Jun", "July": "Jul", "August": "Aug",
            "September": "Sep", "October": "Oct", "November": "Nov", "December": "Dec"
        }
        for full, abbrev in month_abbrevs.items():
            time_str = time_str.replace(full, abbrev)

        # Remove "at" before time: "December 16 at 3:12PM" -> "December 16 3:12PM"
        time_str = re.sub(r'\s+at\s+', ' ', time_str)

        return time_str

    def abbreviate_wind_direction(self, direction: str) -> str:
        """Abbreviate wind direction to emoji + 2-3 characters"""
        if not direction:
            return ""

        direction = direction.upper()
        replacements = {
            "NORTHWEST": "↖️NW",
            "NORTHEAST": "↗️NE",
            "SOUTHWEST": "↙️SW",
            "SOUTHEAST": "↘️SE",
            "NORTH": "⬆️N",
            "EAST": "➡️E",
            "SOUTH": "⬇️S",
            "WEST": "⬅️W"
        }

        for full, abbrev in replacements.items():
            if full in direction:
                return abbrev

        # If no match, return first 2 characters with generic wind emoji
        return f"💨{direction[:2]}" if len(direction) >= 2 else f"💨{direction}"

    def extract_humidity(self, text: str) -> str:
        """Extract humidity percentage from forecast text"""
        if not text:
            return ""

        # Look for patterns like "humidity 45%" or "45% humidity"
        humidity_patterns = [
            r'humidity\s+(\d+)%',
            r'(\d+)%\s+humidity',
            r'relative humidity\s+(\d+)%',
            r'(\d+)%\s+relative humidity'
        ]

        for pattern in humidity_patterns:
            match = re.search(pattern, text.lower())
            if match:
                return match.group(1)

        return ""

    def extract_precip_chance(self, text: str) -> str:
        """Extract precipitation chance from forecast text"""
        if not text:
            return ""

        # Look for patterns like "20% chance" or "chance of rain 30%"
        precip_patterns = [
            r'(\d+)%\s+chance',
            r'chance\s+of\s+\w+\s+(\d+)%',
            r'(\d+)%\s+probability',
            r'probability\s+of\s+\w+\s+(\d+)%'
        ]

        for pattern in precip_patterns:
            match = re.search(pattern, text.lower())
            if match:
                return match.group(1)

        return ""

    def extract_high_low(self, text: str, units_str: str = "°F") -> str:
        """Extract high/low temperatures from forecast text; format via [Weather] templates."""
        if not text:
            return ""

        def _pair_ok(hi: int, lo: int) -> bool:
            if units_str == "°C":
                return -35 <= hi <= 55 and -35 <= lo <= 55 and hi > lo
            return 20 <= hi <= 120 and 20 <= lo <= 120 and hi > lo

        def _single_ok(val: int) -> bool:
            if units_str == "°C":
                return -35 <= val <= 55
            return 20 <= val <= 120

        pair_patterns = [
            r'high\s+near\s+(\d+).*?low\s+around\s+(\d+)',
            r'high\s+(\d+).*?low\s+(\d+)',
            r'(\d+)\s+to\s+(\d+)\s+degrees',
            r'temperature\s+(\d+)\s+to\s+(\d+)',
            r'high\s+near\s+(\d+).*?temperatures\s+falling\s+to\s+around\s+(\d+)',
        ]
        for pattern in pair_patterns:
            match = re.search(pattern, text.lower())
            if match and len(match.groups()) == 2:
                high, low = match.groups()
                try:
                    high_val = int(high)
                    low_val = int(low)
                    if _pair_ok(high_val, low_val):
                        return format_temperature_high_low(
                            self.bot.config, high_val, low_val, units_str, self.logger
                        )
                except ValueError:
                    continue

        low_match = re.search(r'low\s+around\s+(\d+)', text.lower())
        if low_match:
            try:
                low_val = int(low_match.group(1))
                if _single_ok(low_val):
                    return format_temperature_high_low(
                        self.bot.config, None, low_val, units_str, self.logger
                    )
            except ValueError:
                pass

        high_match = re.search(r'high\s+near\s+(\d+)', text.lower())
        if high_match:
            try:
                high_val = int(high_match.group(1))
                if _single_ok(high_val):
                    return format_temperature_high_low(
                        self.bot.config, high_val, None, units_str, self.logger
                    )
            except ValueError:
                pass

        return ""

    def extract_uv_index(self, text: str) -> str:
        """Extract UV index from forecast text"""
        if not text:
            return ""

        # Look for UV index patterns
        uv_patterns = [
            r'uv\s+index\s+(\d+)',
            r'uv\s+(\d+)',
            r'ultraviolet\s+index\s+(\d+)'
        ]

        for pattern in uv_patterns:
            match = re.search(pattern, text.lower())
            if match:
                uv_val = match.group(1)
                # Validate UV index (0-11+ is reasonable)
                try:
                    if 0 <= int(uv_val) <= 15:
                        return uv_val
                except ValueError:
                    continue

        return ""

    def extract_dew_point(self, text: str) -> str:
        """Extract dew point temperature from forecast text"""
        if not text:
            return ""

        # Look for dew point patterns
        dew_point_patterns = [
            r'dew point\s+(\d+)',
            r'dewpoint\s+(\d+)',
            r'dew\s+point\s+(\d+)°'
        ]

        for pattern in dew_point_patterns:
            match = re.search(pattern, text.lower())
            if match:
                dp_val = match.group(1)
                # Validate dew point (reasonable range -20 to 80°F)
                try:
                    if -20 <= int(dp_val) <= 80:
                        return dp_val
                except ValueError:
                    continue

        return ""

    def extract_visibility(self, text: str) -> str:
        """Extract visibility from forecast text"""
        if not text:
            return ""

        # Look for visibility patterns
        visibility_patterns = [
            r'visibility\s+(\d+)\s+miles',
            r'visibility\s+(\d+)\s+mi',
            r'(\d+)\s+mile\s+visibility',
            r'(\d+)\s+mi\s+visibility'
        ]

        for pattern in visibility_patterns:
            match = re.search(pattern, text.lower())
            if match:
                vis_val = match.group(1)
                # Validate visibility (reasonable range 0-20 miles)
                try:
                    if 0 <= int(vis_val) <= 20:
                        return vis_val
                except ValueError:
                    continue

        return ""

    def extract_precip_probability(self, text: str) -> str:
        """Extract precipitation probability from forecast text"""
        if not text:
            return ""

        # Look for precipitation probability patterns
        precip_prob_patterns = [
            r'(\d+)%\s+chance\s+of\s+(?:rain|precipitation|showers)',
            r'chance\s+of\s+(?:rain|precipitation|showers)\s+(\d+)%',
            r'(\d+)%\s+probability\s+of\s+(?:rain|precipitation|showers)',
            r'probability\s+of\s+(?:rain|precipitation|showers)\s+(\d+)%',
            r'(\d+)%\s+chance',
            r'chance\s+(\d+)%'
        ]

        for pattern in precip_prob_patterns:
            match = re.search(pattern, text.lower())
            if match:
                prob_val = match.group(1)
                # Validate probability (0-100%)
                try:
                    if 0 <= int(prob_val) <= 100:
                        return prob_val
                except ValueError:
                    continue

        return ""

    def extract_wind_gusts(self, text: str) -> str:
        """Extract wind gusts from forecast text"""
        if not text:
            return ""

        # Look for wind gust patterns
        gust_patterns = [
            r'gusts\s+to\s+(\d+)\s+mph',
            r'gusts\s+up\s+to\s+(\d+)\s+mph',
            r'wind\s+gusts\s+to\s+(\d+)\s+mph',
            r'wind\s+gusts\s+up\s+to\s+(\d+)\s+mph',
            r'gusts\s+(\d+)\s+mph',
            r'wind\s+gusts\s+(\d+)\s+mph'
        ]

        for pattern in gust_patterns:
            match = re.search(pattern, text.lower())
            if match:
                gust_val = match.group(1)
                # Validate wind gust (reasonable range 10-100 mph)
                try:
                    if 10 <= int(gust_val) <= 100:
                        return gust_val
                except ValueError:
                    continue

        return ""

    def extract_pressure(self, text: str) -> str:
        """Extract barometric pressure from forecast text"""
        if not text:
            return ""

        # Look for pressure patterns (hPa, mb, inches of mercury)
        pressure_patterns = [
            r'pressure\s+(\d+)\s*hpa',
            r'pressure\s+(\d+)\s*mb',
            r'barometric\s+pressure\s+(\d+)\s*hpa',
            r'barometric\s+pressure\s+(\d+)\s*mb',
            r'(\d+)\s*hpa',
            r'(\d+)\s*mb\s+pressure'
        ]

        for pattern in pressure_patterns:
            match = re.search(pattern, text.lower())
            if match:
                pressure_val = match.group(1)
                # Validate pressure (reasonable range 600-1100 hPa/mb)
                # Normal sea level is ~1013 hPa, but high elevation locations can be lower
                try:
                    pressure_int = int(pressure_val)
                    if 600 <= pressure_int <= 1100:
                        return pressure_val
                except ValueError:
                    continue

        return ""

    def get_observation_data(self, points_data: dict) -> dict:
        """Get observation station data from NOAA and return as a dict

        Returns:
            Dict with keys: temperature, feels_like, humidity, dew_point, visibility, wind_gusts, pressure
            Values are strings ready for display, or None if not available
        """
        try:
            if not points_data:
                return {}

            weather_json = points_data
            station_url = weather_json['properties'].get('observationStations')
            if not station_url:
                return {}

            # Get the nearest station (with retry logic)
            # Use shorter timeout for optional observation data to avoid blocking main response
            obs_timeout = min(self.url_timeout, 5)  # Cap at 5 seconds for optional data
            try:
                stations_data = self.noaa_session.get(station_url, timeout=obs_timeout)
                if not stations_data.ok:
                    return {}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                return {}

            stations_json = stations_data.json()
            if not stations_json.get('features'):
                return {}

            # The nearest station's latest ob sometimes reports null humidity/
            # dewpoint (or fails the fetch); try the next-nearest stations to fill
            # the gaps so a message never shows a lonely "wind ..." line.
            obs_data_dict: dict = {}
            for feature in (stations_json.get('features') or [])[:3]:
                try:
                    station_id = feature['properties']['stationIdentifier']
                except (KeyError, TypeError):
                    continue
                obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
                try:
                    obs_data = self.noaa_session.get(obs_url, timeout=obs_timeout)
                    if not obs_data.ok:
                        continue
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                    continue

                props = (obs_data.json() or {}).get('properties') or {}

                # Fill each field from the first station that reports it (non-null).
                if 'temperature' not in obs_data_dict:
                    val = props.get('temperature', {}).get('value')
                    if val is not None:
                        obs_data_dict['temperature'] = str(round(val * 9 / 5 + 32))  # °C -> °F (the live "now")
                if 'humidity' not in obs_data_dict:
                    val = props.get('relativeHumidity', {}).get('value')
                    if val is not None:
                        obs_data_dict['humidity'] = str(int(val))
                if 'dew_point' not in obs_data_dict:
                    val = props.get('dewpoint', {}).get('value')
                    if val is not None:
                        obs_data_dict['dew_point'] = str(int(val * 9/5 + 32))  # C -> F
                if 'feels_like' not in obs_data_dict:
                    # NOAA obs carries heatIndex (hot) or windChill (cold).
                    val = props.get('heatIndex', {}).get('value')
                    if val is None:
                        val = props.get('windChill', {}).get('value')
                    if val is not None:
                        obs_data_dict['feels_like'] = str(int(val * 9/5 + 32))  # C -> F
                if 'visibility' not in obs_data_dict:
                    val = props.get('visibility', {}).get('value')
                    if val is not None:
                        miles = int(val * 0.000621371)  # m -> miles
                        if miles > 0:
                            obs_data_dict['visibility'] = str(miles)
                if 'wind_gusts' not in obs_data_dict:
                    val = props.get('windGust', {}).get('value')
                    if val is not None:
                        gust = int(val * 2.237)  # m/s -> mph
                        if gust > 10:
                            obs_data_dict['wind_gusts'] = str(gust)
                if 'pressure' not in obs_data_dict:
                    val = props.get('barometricPressure', {}).get('value')
                    if val is not None:
                        obs_data_dict['pressure'] = str(int(val / 100))  # Pa -> hPa

                # The fields that matter most for the display are present — stop early.
                if ('temperature' in obs_data_dict and 'humidity' in obs_data_dict
                        and 'dew_point' in obs_data_dict):
                    break

            return obs_data_dict

        except Exception as e:
            self.logger.debug(f"Error getting observation data: {e}")
            return {}

    def get_current_conditions(self, points_data: dict) -> str:
        """Get additional current conditions data from NOAA using existing points data (legacy method)"""
        obs_data = self.get_observation_data(points_data)
        if not obs_data:
            return ""

        conditions = []

        # Build conditions list in priority order
        if 'humidity' in obs_data:
            conditions.append(f"{obs_data['humidity']}%RH")

        if 'dew_point' in obs_data:
            conditions.append(f"💧{obs_data['dew_point']}°")

        if 'visibility' in obs_data:
            conditions.append(f"👁️{obs_data['visibility']}mi")

        if 'wind_gusts' in obs_data:
            conditions.append(f"💨{obs_data['wind_gusts']}")

        if 'pressure' in obs_data:
            conditions.append(f"📊{obs_data['pressure']}hPa")

        return " ".join(conditions[:3])  # Limit to 3 conditions to avoid overflow

    def get_weather_emoji(self, condition: str) -> str:
        """Get emoji for weather condition"""
        if not condition:
            return ""

        condition_lower = condition.lower()

        # Weather condition emojis
        if any(word in condition_lower for word in ['sunny', 'clear']):
            return "☀️"
        elif any(word in condition_lower for word in ['heavy rain', 'heavy showers', 'excessive rain']):
            return "🌧️"  # Cloud with rain - more rain, less sun
        elif any(word in condition_lower for word in ['cloudy', 'overcast']):
            return "☁️"
        elif any(word in condition_lower for word in ['partly cloudy', 'mostly cloudy']):
            return "⛅"
        elif any(word in condition_lower for word in ['rain', 'showers']):
            return "🌦️"
        elif any(word in condition_lower for word in ['thunderstorm', 'thunderstorms']):
            return "⛈️"
        elif any(word in condition_lower for word in ['snow', 'snow showers']):
            return "❄️"
        elif any(word in condition_lower for word in ['fog', 'mist', 'haze']):
            return "🌫️"
        elif any(word in condition_lower for word in ['smoke']) or any(word in condition_lower for word in ['windy', 'breezy']):
            return "💨"
        else:
            return "🌤️"  # Default weather emoji

    # NOAA sometimes names forecast periods after federal holidays (e.g. "Washington's Birthday")
    # instead of the weekday. Match these so we can resolve to weekday via startTime.
    _NOAA_HOLIDAY_NAME_PATTERNS = (
        "washington's birthday", "presidents day", "president's day",
        "martin luther king", "mlk day", "memorial day", "labor day",
        "independence day", "juneteenth", "columbus day", "veterans day",
        "thanksgiving", "christmas day", "new year's day", "new year's eve",
    )

    def _noaa_period_display_name(self, period: dict) -> str:
        """Return display label for a NOAA forecast period. Resolves holiday names to weekday."""
        name = period.get('name', '') or ''
        start_time_str = period.get('startTime')
        name_lower = name.lower()
        is_holiday = any(p in name_lower for p in self._NOAA_HOLIDAY_NAME_PATTERNS)
        if is_holiday and start_time_str:
            try:
                # startTime is ISO 8601, e.g. 2025-02-17T08:00:00-08:00
                dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                # Python weekday(): Mon=0 .. Sun=6
                weekdays = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
                day_abbrev = weekdays[dt.weekday()]
                if 'night' in name_lower or 'overnight' in name_lower:
                    return f"{day_abbrev} Night"
                return day_abbrev
            except (ValueError, TypeError):
                pass
        return self.abbreviate_noaa(name)

    def abbreviate_noaa(self, text: str) -> str:
        """Replace long strings with shorter ones for display"""
        replacements = {
            "monday": "Mon",
            "tuesday": "Tue",
            "wednesday": "Wed",
            "thursday": "Thu",
            "friday": "Fri",
            "saturday": "Sat",
            "sunday": "Sun",
            "northwest": "NW",
            "northeast": "NE",
            "southwest": "SW",
            "southeast": "SE",
            "north": "N",
            "south": "S",
            "east": "E",
            "west": "W",
            "precipitation": "precip",
            "showers": "shwrs",
            "thunderstorms": "t-storms",
            "thunderstorm": "t-storm",
            "quarters": "qtrs",
            "quarter": "qtr",
            "january": "Jan",
            "february": "Feb",
            "march": "Mar",
            "april": "Apr",
            "may": "May",
            "june": "Jun",
            "july": "Jul",
            "august": "Aug",
            "september": "Sep",
            "october": "Oct",
            "november": "Nov",
            "december": "Dec",
            "degrees": "°",
            "percent": "%",
            "department": "Dept.",
            "amounts less than a tenth of an inch possible.": "< 0.1in",
            "temperatures": "temps.",
            "temperature": "temp.",
        }

        line = text
        for key, value in replacements.items():
            # Case insensitive replace
            line = line.replace(key, value).replace(key.capitalize(), value).replace(key.upper(), value)

        return line
