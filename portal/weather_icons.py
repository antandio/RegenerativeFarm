"""Task 6: pure local WMO weather-code to icon/label mapping.

No network, no provider data beyond the already-fetched `weather_code` /
`is_day` fields. Strict: only real `int` codes map (Python `bool` is
rejected even though it subclasses `int`); strings, None and unknown ints
fall back to the neutral icon. Known day is exactly `int` 1, known night
exactly `int` 0; a missing or invalid `is_day` for clear-ish codes renders
a dedicated neutral glyph (no sun rays, no moon crescent) with the exact
valid condition label. Daily summaries always use day icons via exact
`int` 1 (daily payloads carry no `is_day`).
"""
from __future__ import annotations

from django.utils.translation import gettext as _

UNKNOWN_ICON = "wx-unknown"
UNKNOWN_LABEL = "Nem elérhető"

ICON_IDS = frozenset({
    "wx-clear", "wx-mainly-clear", "wx-partly-cloudy",
    "wx-clear-day", "wx-clear-night",
    "wx-mainly-clear-day", "wx-mainly-clear-night",
    "wx-partly-cloudy-day", "wx-partly-cloudy-night",
    "wx-overcast", "wx-fog", "wx-drizzle", "wx-freezing-drizzle",
    "wx-rain", "wx-freezing-rain", "wx-snow", "wx-snow-grains",
    "wx-showers", "wx-snow-showers", "wx-thunderstorm", "wx-hail",
    UNKNOWN_ICON,
})

# code -> (day icon id, night icon id or None, HU label msgid)
_GROUPS = {
    0: ("wx-clear-day", "wx-clear-night", "Derült"),
    1: ("wx-mainly-clear-day", "wx-mainly-clear-night", "Többnyire derült"),
    2: ("wx-partly-cloudy-day", "wx-partly-cloudy-night", "Változóan felhős"),
    3: ("wx-overcast", None, "Borult"),
    45: ("wx-fog", None, "Köd"),
    48: ("wx-fog", None, "Köd"),
    51: ("wx-drizzle", None, "Szitálás"),
    53: ("wx-drizzle", None, "Szitálás"),
    55: ("wx-drizzle", None, "Szitálás"),
    56: ("wx-freezing-drizzle", None, "Ónos szitálás"),
    57: ("wx-freezing-drizzle", None, "Ónos szitálás"),
    61: ("wx-rain", None, "Eső"),
    63: ("wx-rain", None, "Eső"),
    65: ("wx-rain", None, "Eső"),
    66: ("wx-freezing-rain", None, "Ónos eső"),
    67: ("wx-freezing-rain", None, "Ónos eső"),
    71: ("wx-snow", None, "Havazás"),
    73: ("wx-snow", None, "Havazás"),
    75: ("wx-snow", None, "Havazás"),
    77: ("wx-snow-grains", None, "Hódara"),
    80: ("wx-showers", None, "Zápor"),
    81: ("wx-showers", None, "Zápor"),
    82: ("wx-showers", None, "Zápor"),
    85: ("wx-snow-showers", None, "Hózápor"),
    86: ("wx-snow-showers", None, "Hózápor"),
    95: ("wx-thunderstorm", None, "Zivatar"),
    96: ("wx-hail", None, "Jégeső"),
    99: ("wx-hail", None, "Jégeső"),
}


def unknown():
    """Neutral fallback for missing/invalid codes."""
    return (UNKNOWN_ICON, _(UNKNOWN_LABEL))


_NEUTRAL = {0: "wx-clear", 1: "wx-mainly-clear", 2: "wx-partly-cloudy"}


def _is_night(is_day):
    return type(is_day) is int and is_day == 0


def _is_day(is_day):
    return type(is_day) is int and is_day == 1


def icon_for(code, is_day=None):
    """Return (icon_id, translated label) for a WMO code.

    Known day is exactly `int` 1, known night exactly `int` 0. For
    clear-ish codes (0/1/2) any other `is_day` (missing, invalid such as
    2, bool, str, float) returns the dedicated neutral glyph with the
    exact valid condition label, never a sun/moon invention.
    """
    if type(code) is not int:
        return unknown()
    entry = _GROUPS.get(code)
    if entry is None:
        return unknown()
    day_id, night_id, label = entry
    if code in _NEUTRAL:
        if _is_night(is_day) and night_id is not None:
            return (night_id, _(label))
        if _is_day(is_day):
            return (day_id, _(label))
        return (_NEUTRAL[code], _(label))
    if _is_night(is_day) and night_id is not None:
        return (night_id, _(label))
    return (day_id, _(label))


def icon_for_day(code):
    """Day-summary icon for a daily row (no night claim)."""
    icon_id, label = icon_for(code, 1)
    return (icon_id, label)
