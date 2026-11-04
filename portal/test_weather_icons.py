"""Task 6 weather pictograms tests (RED-first): strict WMO mapping + parser fields.

Covers: exact Open-Meteo query fields on the SAME host, actual
`weather_code` key parsing (never `weathercode`), every WMO code mapping,
strict int-only codes, exact is_day day/night rules, old-cache neutral
render with no network, HU/EN accessible labels, no icons on climate.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
import httpx

from portal import weather as W
from portal.models import Farm, FarmLocation

try:
    from portal import weather_icons as WI
except ImportError:
    WI = None

User = get_user_model()
PW = "HosszuJelszo123!"


def _mkuser(email="wx@example.com"):
    return User.objects.create_user(email=email, password=PW)


def _farm_loc(owner):
    f = Farm.objects.create(owner=owner, name="Wx Farm")
    loc = FarmLocation.objects.create(
        farm=f, name="H", latitude="46.250000", longitude="20.150000",
        timezone="Europe/Budapest")
    return f, loc


def _new_body():
    return {
        "current": {"time": "2026-01-01T12:00", "temperature_2m": 5.0,
                    "precipitation": 0.2, "wind_speed_10m": 12.0,
                    "weather_code": 2, "is_day": 1},
        "current_units": {"temperature_2m": "°C", "precipitation": "mm",
                          "wind_speed_10m": "km/h"},
        "daily": {"time": ["2026-01-0%d" % i for i in range(1, 8)],
                  "temperature_2m_max": [6.0] * 7,
                  "temperature_2m_min": [-1.0] * 7,
                  "precipitation_sum": [0.5] * 7,
                  "wind_speed_10m_max": [15.0] * 7,
                  "weather_code": [0, 1, 2, 3, 45, 61, 95]},
    }


def _old_body():
    return {"current": {"time": "2026-01-01T12:00", "temperature_2m": 5.0,
                        "precipitation": 0.2, "wind_speed_10m": 12.0},
            "current_units": {"temperature_2m": "°C", "precipitation": "mm",
                              "wind_speed_10m": "km/h"},
            "daily": {"time": ["2026-01-0%d" % i for i in range(1, 8)],
                      "temperature_2m_max": [6.0] * 7,
                      "temperature_2m_min": [-1.0] * 7,
                      "precipitation_sum": [0.5] * 7,
                      "wind_speed_10m_max": [15.0] * 7}}


def _transport_capture(body, seen):
    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=body)
    return httpx.MockTransport(h)


class WeatherIconParamTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_forecast_requests_exact_code_fields_same_host(self):
        seen = []
        W.fetch_forecast(46.25, 20.15, "Europe/Budapest",
                         transport=_transport_capture(_new_body(), seen))
        self.assertEqual(len(seen), 1)
        url = seen[0]
        self.assertTrue(url.startswith(W.FORECAST_URL), url)
        self.assertIn("weather_code", url)
        self.assertIn("is_day", url)

    def test_parser_carries_actual_weather_code_key(self):
        seen = []
        p = W.fetch_forecast(46.25, 20.15, "Europe/Budapest",
                             transport=_transport_capture(_new_body(), seen))
        self.assertEqual((p["current"] or {}).get("weather_code"), 2)
        self.assertEqual((p["current"] or {}).get("is_day"), 1)
        self.assertEqual(
            [(d or {}).get("weather_code") for d in p["daily"]],
            [0, 1, 2, 3, 45, 61, 95])

    def test_parser_old_shape_without_codes_is_safe_neutral(self):
        seen = []
        p = W.fetch_forecast(46.25, 20.15, "Europe/Budapest",
                             transport=_transport_capture(_old_body(), seen))
        self.assertIsNone((p["current"] or {}).get("weather_code"))
        self.assertIsNone((p["current"] or {}).get("is_day"))
        self.assertTrue(all((d or {}).get("weather_code") is None for d in p["daily"]))


class WeatherIconMappingTests(TestCase):
    EXPECTED = {0: "wx-clear-day", 1: "wx-mainly-clear-day",
                2: "wx-partly-cloudy-day", 3: "wx-overcast",
                45: "wx-fog", 48: "wx-fog",
                51: "wx-drizzle", 53: "wx-drizzle", 55: "wx-drizzle",
                56: "wx-freezing-drizzle", 57: "wx-freezing-drizzle",
                61: "wx-rain", 63: "wx-rain", 65: "wx-rain",
                66: "wx-freezing-rain", 67: "wx-freezing-rain",
                71: "wx-snow", 73: "wx-snow", 75: "wx-snow",
                77: "wx-snow-grains",
                80: "wx-showers", 81: "wx-showers", 82: "wx-showers",
                85: "wx-snow-showers", 86: "wx-snow-showers",
                95: "wx-thunderstorm", 96: "wx-hail", 99: "wx-hail"}

    def test_module_present(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")

    def test_every_wmo_code_maps_to_allowlisted_id(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")
        for code, icon_id in self.EXPECTED.items():
            with self.subTest(code=code):
                got_id, label = WI.icon_for(code, 1)
                self.assertEqual(got_id, icon_id)
                self.assertIn(got_id, WI.ICON_IDS)
                self.assertTrue(isinstance(label, str) and len(label) > 0)

    def test_labels_are_distinct_per_group_hu(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")
        labels = set()
        for code in [0, 1, 2, 3, 45, 51, 56, 61, 66, 71, 77, 80, 85, 95, 96]:
            _i, label = WI.icon_for(code, 1)
            labels.add(label)
        self.assertEqual(len(labels), 15)

    def test_strict_int_only_rejects_bool_str_none_unknown(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")
        for bad in [True, False, "3", "0", None, 999, -1, 3.5, [3]]:
            with self.subTest(bad=repr(bad)):
                got_id, _label = WI.icon_for(bad, 1)
                self.assertEqual(got_id, "wx-unknown")

    def test_night_only_for_exact_int_zero_current(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")
        day_id, _ = WI.icon_for(0, 1)
        night_id, _ = WI.icon_for(0, 0)
        self.assertEqual(day_id, "wx-clear-day")
        self.assertEqual(night_id, "wx-clear-night")
        # bool False == 0 but must NOT count as night (strict int); invalid -> neutral wx-clear
        for bad_day in [None, "0", True, False, 1.0]:
            with self.subTest(is_day=repr(bad_day)):
                got_id, _ = WI.icon_for(0, bad_day)
                self.assertEqual(got_id, "wx-clear")

    def test_daily_always_day_summary(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")
        for code in [0, 1, 2, 3, 45, 95]:
            with self.subTest(code=code):
                self.assertEqual(WI.icon_for_day(code)[0],
                                 WI.icon_for(code, 1)[0])


class WeatherIconViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = _mkuser()
        self.f, self.loc = _farm_loc(self.u)
        self.client.force_login(self.u)

    def _seed_cache(self, payload):
        import time
        cache.set(W._ck("forecast", self.f.id, self.loc),
                  {"payload": payload, "stored_at": time.time()}, 3600)

    def _payload(self, code, is_day, daily_codes):
        import copy
        p = W.fetch_forecast(46.25, 20.15, "Europe/Budapest",
                             transport=_transport_capture(_new_body(), []))
        p = copy.deepcopy(p)
        p["current"]["weather_code"] = code
        p["current"]["is_day"] = is_day
        for d, c in zip(p["daily"], daily_codes):
            d["weather_code"] = c
        return p

    def test_old_cached_payload_renders_neutral_no_network(self):
        import copy
        from unittest.mock import patch
        seen = []
        p = W.fetch_forecast(46.25, 20.15, "Europe/Budapest",
                             transport=_transport_capture(_old_body(), seen))
        self._seed_cache(copy.deepcopy(p))
        with patch("portal.weather.httpx.get",
                   side_effect=AssertionError("no network on GET")):
            r = self.client.get(reverse("weather"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("wx-unknown", b)
        self.assertIn("Nem elérhető", b)
        self.assertIn("Open-Meteo", b)

    def test_current_day_icon_and_label_hu(self):
        self._seed_cache(self._payload(0, 1, [1, 2, 3, 45, 51, 61, 95]))
        b = self.client.get(reverse("weather")).content.decode()
        self.assertIn("wx-clear-day", b)
        self.assertIn("Derült", b)
        self.assertIn('aria-hidden="true"', b)

    def test_current_night_icon_hu(self):
        self._seed_cache(self._payload(2, 0, [0, 1, 2, 3, 45, 61, 95]))
        b = self.client.get(reverse("weather")).content.decode()
        self.assertIn("wx-partly-cloudy-night", b)
        self.assertNotIn("wx-partly-cloudy-day", b.split("Aktuális:")[1].split("</p>")[0])

    def test_daily_rows_show_day_icons_and_labels(self):
        self._seed_cache(self._payload(3, 1, [0, 1, 2, 3, 45, 61, 95]))
        b = self.client.get(reverse("weather")).content.decode()
        for token in ["wx-mainly-clear-day", "wx-overcast", "wx-fog",
                      "wx-rain", "wx-thunderstorm", "Zivatar", "Borult"]:
            self.assertIn(token, b)

    def test_english_labels(self):
        self._seed_cache(self._payload(0, 1, [2, 3, 45, 51, 61, 71, 95]))
        self.client.cookies["django_language"] = "en"
        b = self.client.get(reverse("weather")).content.decode()
        self.assertIn("Clear sky", b)
        self.assertIn("Partly cloudy", b)

    def test_no_forecast_renders_neutral_with_existing_warning(self):
        r = self.client.get(reverse("weather"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("wx-unknown", b)
        self.assertIn("Előrejelzés hiányzik", b)

    def test_no_icons_on_climate_and_no_emoji(self):
        import time
        self._seed_cache(self._payload(0, 1, [0, 1, 2, 3, 45, 61, 95]))
        cache.set(W._ck("climate", self.f.id, self.loc),
                  {"payload": {
                      "period": "2016-2025", "years": 10,
                      "monthly": [{"month": m, "tmax_mean": 10.0,
                                   "tmin_mean": 0.0, "precip_mean": 1.0}
                                  for m in range(1, 13)],
                      "label": "historical climate baseline (NOT forecast)",
                      "limitation": "10-year monthly baseline",
                      "source": W.SOURCE, "license": W.LICENSE,
                      "fetched_at": "2026-01-01T00:00:00+00:00"},
                   "stored_at": time.time()}, 3600)
        b = self.client.get(reverse("weather")).content.decode()
        self.assertIn("Havi éghajlati átlagok", b)
        climate = b.split("Havi éghajlati átlagok")[1]
        self.assertNotIn("wx-", climate)
        self.assertNotIn("<svg", climate)
        for ch in ["\U0001f324", "\U0001f327", "\U0001f329", "\U0001f319", "\u2600", "\u26c5"]:
            self.assertNotIn(ch, b)


class WeatherIconDayUnknownNeutralTests(TestCase):
    """RED: valid code + unknown/invalid is_day must NOT invent sun/moon.

    Known day is exactly int 1, known night exactly int 0; anything else
    uses dedicated neutral glyphs with the exact valid condition label.
    """

    def setUp(self):
        self.assertIsNotNone(WI, "portal/weather_icons.py missing")

    def test_clear_unknown_is_day_is_neutral_not_day_night(self):
        got_id, label = WI.icon_for(0, None)
        self.assertNotIn(got_id, ("wx-clear-day", "wx-clear-night"))
        self.assertEqual(got_id, "wx-clear")
        self.assertEqual(label, WI.icon_for(0, 1)[1])

    def test_clear_invalid_is_day_two_does_not_claim_sun(self):
        for bad in [2, -1, True, False, "1", 1.0, "0", None]:
            with self.subTest(is_day=repr(bad)):
                got_id, _label = WI.icon_for(0, bad)
                self.assertEqual(got_id, "wx-clear")
                self.assertNotIn(got_id, ("wx-clear-day", "wx-clear-night"))

    def test_mainly_partly_unknown_is_day_neutral_cloud_no_sun_moon(self):
        self.assertEqual(WI.icon_for(1, None)[0], "wx-mainly-clear")
        self.assertEqual(WI.icon_for(2, None)[0], "wx-partly-cloudy")
        self.assertEqual(WI.icon_for(1, 2)[0], "wx-mainly-clear")
        self.assertEqual(WI.icon_for(2, 2)[0], "wx-partly-cloudy")
        for got, day_code in [(WI.icon_for(1, None)[0], 1),
                              (WI.icon_for(2, None)[0], 2)]:
            self.assertNotIn("day", got)
            self.assertNotIn("night", got)
            self.assertNotIn("sun", got)
            self.assertNotIn("moon", got)
        self.assertEqual(WI.icon_for(1, None)[1], WI.icon_for(1, 1)[1])
        self.assertEqual(WI.icon_for(2, None)[1], WI.icon_for(2, 1)[1])

    def test_neutral_ids_allowlisted_and_sprite_has_symbols(self):
        import pathlib
        for nid in ["wx-clear", "wx-mainly-clear", "wx-partly-cloudy"]:
            self.assertIn(nid, WI.ICON_IDS)
        sprite = pathlib.Path("portal/templates/portal/_weather_icons.html").read_text()
        for nid in ["wx-clear", "wx-mainly-clear", "wx-partly-cloudy"]:
            self.assertIn('id="%s"' % nid, sprite)


class WeatherOutdatedNoneNeutralTests(TestCase):
    """RED: expired cache (None,'outdated') renders neutral + stale warning."""

    def setUp(self):
        cache.clear()
        self.u = _mkuser("outdated@example.com")
        self.f, self.loc = _farm_loc(self.u)
        self.client.force_login(self.u)

    def test_outdated_none_shows_unknown_and_stale_warning(self):
        import time
        from unittest.mock import patch
        with patch("portal.weather.httpx.get",
                   side_effect=AssertionError("no network on GET")):
            r = self.client.get(reverse("weather"))
        # unseeded view is missing-neutral; now force expired entry path
        self.assertEqual(r.status_code, 200)
        # Simulate expired cache entry: payload present but stored long ago
        # so cached() returns (None, 'outdated') and view has no forecast.
        old = {"payload": {"current": {"weather_code": 0, "is_day": 1},
                           "daily": [], "units": {},
                           "source": "x", "license": "y",
                           "fetched_at": "t"},
               "stored_at": time.time() - W.STALE_TTL - 10}
        cache.set(W._ck("forecast", self.f.id, self.loc), old, 3600)
        payload, status = W.cached("forecast", self.f.id, self.loc)
        self.assertIsNone(payload)
        self.assertEqual(status, "outdated")
        with patch("portal.weather.httpx.get",
                   side_effect=AssertionError("no network on GET")):
            r2 = self.client.get(reverse("weather"))
        self.assertEqual(r2.status_code, 200)
        b = r2.content.decode()
        self.assertIn("wx-unknown", b)
        self.assertIn("Nem elérhető", b)
        self.assertIn("Gyorsítótárazott / elavult adat.", b)
        self.assertIn("outdated", b)
