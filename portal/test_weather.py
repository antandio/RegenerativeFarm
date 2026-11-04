"""Stage 4 weather tests (RED-first): httpx MockTransport, cache/stale/missing/
invalid-coords/timeout, tenant page readonly except explicit owner/manager POST."""
import json
from django.core.cache import cache
from django.test import TestCase
from django.contrib.auth import get_user_model
import httpx
from portal import weather as W
from portal.models import Farm, Membership, FarmLocation

User = get_user_model()


def _mkuser(email="w@example.com", pw="HosszuJelszo123"):
    return User.objects.create_user(email=email, password=pw)


def _farm_loc(owner, lat="46.250000", lon="20.150000", tz="Europe/Budapest"):
    f = Farm.objects.create(owner=owner, name="F")
    loc = FarmLocation.objects.create(farm=f, name="H", latitude=lat, longitude=lon, timezone=tz)
    return f, loc


def _forecast_body():
    return {"current": {"time": "2026-01-01T12:00", "temperature_2m": 5.0,
                        "precipitation": 0.2, "wind_speed_10m": 12.0},
            "current_units": {"temperature_2m": "°C", "precipitation": "mm",
                              "wind_speed_10m": "km/h"},
            "daily": {"time": ["2026-01-0%d" % i for i in range(1, 8)],
                      "temperature_2m_max": [6.0] * 7, "temperature_2m_min": [-1.0] * 7,
                      "precipitation_sum": [0.5] * 7, "wind_speed_10m_max": [15.0] * 7}}


def _transport(body=None, exc=None):
    def h(req):
        if exc:
            raise exc
        return httpx.Response(200, json=body if body is not None else _forecast_body())
    return httpx.MockTransport(h)


def _archive_body():
    days, tx, tn, pr = [], [], [], []
    for m in range(1, 13):
        for dd in range(1, 29):
            days.append(f"2020-{m:02d}-{dd:02d}")
            tx.append(10.0 + m)
            tn.append(float(m))
            pr.append(1.0)
    return {"daily": {"time": days, "temperature_2m_max": tx,
                      "temperature_2m_min": tn, "precipitation_sum": pr}}


class WeatherUnitTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_invalid_coords_rejected_no_url(self):
        for la, lo in [("999", "0"), ("0", "999"), ("x", "0")]:
            with self.assertRaises(W.WeatherError):
                W.fetch_forecast(la, lo, "Europe/Budapest",
                                 transport=_transport())
        self.assertEqual(W.FORECAST_URL, "https://api.open-meteo.com/v1/forecast")
        self.assertEqual(W.ARCHIVE_URL, "https://archive-api.open-meteo.com/v1/archive")

    def test_forecast_parses_current_and_7day(self):
        p = W.fetch_forecast(46.25, 20.15, "Europe/Budapest", transport=_transport())
        self.assertEqual(p["current"]["temperature"], 5.0)
        self.assertEqual(len(p["daily"]), 7)
        self.assertIn("fetched_at", p)
        self.assertIn("source", p)

    def test_timeout_maps_to_missing_with_stale_fallback(self):
        u = _mkuser()
        f, loc = _farm_loc(u)
        W.refresh_forecast(f.id, loc, transport=_transport())
        old, st = W.get_forecast_cached(f.id, loc)
        self.assertEqual(st, "fresh")
        p, st2 = W.refresh_forecast(
            f.id, loc, transport=_transport(exc=httpx.ConnectTimeout("t")))
        self.assertIsNotNone(p)
        self.assertIn(st2, ("stale", "outdated-fallback"))

    def test_timeout_missing_cache_gives_missing(self):
        u = _mkuser(email="w2@example.com")
        f, loc = _farm_loc(u)
        p, st = W.refresh_forecast(
            f.id, loc, transport=_transport(exc=httpx.ConnectTimeout("t")))
        self.assertIsNone(p)
        self.assertEqual(st, "missing")

    def test_climate_monthly_aggregated_real(self):
        def h(req):
            return httpx.Response(200, json=_archive_body())
        p = W.fetch_climate(46.25, 20.15, "Europe/Budapest", years=1,
                            transport=httpx.MockTransport(h))
        self.assertEqual(len(p["monthly"]), 12)
        self.assertEqual(p["monthly"][0]["tmax_mean"], 11.0)
        self.assertIn("NOT forecast", p["label"])
        self.assertIn("not", p["limitation"].lower())

    def test_plan_notes_caution_no_guarantee(self):
        p = W.fetch_forecast(46.25, 20.15, "Europe/Budapest", transport=_transport())
        p["daily"][0]["precip_sum"] = 15.0
        p["daily"][0]["wind_max"] = 50.0
        notes = W.plan_notes(p)
        text = " ".join(notes)
        self.assertIn("garancia nincs", text)
        self.assertIn("korlát", text)


class WeatherViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = _mkuser()
        self.f, self.loc = _farm_loc(self.u)
        self.client.force_login(self.u)

    def test_get_cache_only_no_network_no_db_write(self):
        n = FarmLocation.objects.count()
        r = self.client.get("/weather/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FarmLocation.objects.count(), n)
        self.assertContains(r, "Open-Meteo")

    def test_get_does_not_fetch_network(self):
        def boom(req):
            raise AssertionError("GET must not hit network")
        # warm cache first via explicit refresh path logic with good transport
        W.refresh_forecast(self.f.id, self.loc, transport=_transport())
        from unittest.mock import patch
        with patch("portal.weather.httpx.get", side_effect=AssertionError("no net")):
            r = self.client.get("/weather/")
            self.assertEqual(r.status_code, 200)

    def test_refresh_post_requires_owner_manager(self):
        viewer = User.objects.create_user(email="v@example.com", password="HosszuJelszo123")
        Membership.objects.create(farm=self.f, user=viewer, role="viewer", is_active=True)
        self.client.force_login(viewer)
        r = self.client.post("/weather/refresh/", {"scope": "current"})
        self.assertEqual(r.status_code, 403)
        self.client.force_login(self.u)
        r = self.client.post("/weather/refresh/", {"scope": "current"})
        self.assertIn(r.status_code, (302, 200))
