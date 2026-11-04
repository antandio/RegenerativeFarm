"""Stage 4 integration RED: generated plan carries weather_annotations built
from CACHED location data only (no network): per-item timestamp + stale /
missing risk note, and a climate evidence summary (NOT forecast)."""
from datetime import date

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from portal import weather as W
from portal.models import CropCycle, Farm, FarmLocation, FarmProperty
from portal.planning import generate_plan

User = get_user_model()
PW = "TesztJelszo123!"


def _forecast_body():
    return {"current": {"time": "2026-03-02T12:00", "temperature_2m": 8.0,
                        "precipitation": 0.0, "wind_speed_10m": 10.0},
            "current_units": {"temperature_2m": "°C", "precipitation": "mm",
                              "wind_speed_10m": "km/h"},
            "daily": {"time": ["2026-03-0%d" % i for i in range(1, 8)],
                      "temperature_2m_max": [9.0] * 7, "temperature_2m_min": [-2.0] * 7,
                      "precipitation_sum": [12.0] + [0.0] * 6,
                      "wind_speed_10m_max": [45.0] + [10.0] * 6}}


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


def _transport_for(url_kind):
    def h(req):
        if "archive-api" in str(req.url):
            return httpx.Response(200, json=_archive_body())
        return httpx.Response(200, json=_forecast_body())
    return httpx.MockTransport(h)


class PlanWeatherIntegrationTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_plan_weather_annotations_from_cache_only(self):
        u = User.objects.create_user(email="pw@example.com", password=PW)
        f = Farm.objects.create(owner=u, name="F")
        loc = FarmLocation.objects.create(
            farm=f, name="L", latitude="46.250000",
            longitude="20.150000", timezone="Europe/Budapest")
        prop = FarmProperty.objects.create(
            farm=f, location=loc, name="P", kind="field", area_hectares="1")
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Buza", state="current",
            sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
        # Warm the cache through the explicit refresh path (MockTransport).
        tr = _transport_for("both")
        W.refresh_forecast(f.id, loc, transport=tr)
        W.refresh_climate(f.id, loc, years=1, transport=tr)
        # Plans view builds this map CACHE-ONLY (no network here):
        from unittest.mock import patch
        with patch("portal.weather.httpx.get",
                   side_effect=AssertionError("plan must not hit network")):
            fc, fstatus = W.get_forecast_cached(f.id, loc)
            cl, cstatus = W.get_climate_cached(f.id, loc)
        weather = {prop.id: {"forecast": fc, "fstatus": fstatus,
                             "climate": cl, "cstatus": cstatus}}
        cycles = [{"id": cyc.id, "property_id": prop.id, "crop_name": "Buza",
                   "sow_date": cyc.sow_date,
                   "expected_end_date": cyc.expected_end_date}]
        items, summary = generate_plan(cycles, [], weather=weather)
        self.assertEqual(len(items), 4)
        for it in items:
            notes = it.get("weather_annotations")
            self.assertIsInstance(notes, list, "per-item weather_annotations missing")
            text = " ".join(notes)
            # Timestamp of the cached payload.
            self.assertIn(fc["fetched_at"], text)
            # Units shown.
            self.assertIn("mm", text)
            self.assertIn("km/h", text)
            # Stale/missing risk wording (fresh here -> risk caveat still present).
            self.assertTrue("stale" in text.lower() or "elavult" in text
                            or "kock" in text or "garancia" in text,
                            "stale/missing risk note missing: %r" % text)
            # Climate evidence summary attached per item (NOT forecast).
            self.assertIn("NOT forecast", text)
        # Summary-level climate evidence.
        ev = summary.get("weather_evidence")
        self.assertIsInstance(ev, dict)
        entry = ev.get(prop.id)
        self.assertIsNotNone(entry)
        self.assertIn("climate_period", entry)
        self.assertIn("limitation", entry)
        self.assertIn("fetched_at", entry)

    def test_plan_weather_missing_cache_marks_missing(self):
        u = User.objects.create_user(email="pw2@example.com", password=PW)
        f = Farm.objects.create(owner=u, name="F2")
        loc = FarmLocation.objects.create(
            farm=f, name="L2", latitude="46.250000",
            longitude="20.150000", timezone="Europe/Budapest")
        prop = FarmProperty.objects.create(
            farm=f, location=loc, name="P2", kind="field", area_hectares="1")
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Buza", state="current",
            sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
        fc, fstatus = W.get_forecast_cached(f.id, loc)
        cl, cstatus = W.get_climate_cached(f.id, loc)
        self.assertEqual(fstatus, "missing")
        weather = {prop.id: {"forecast": fc, "fstatus": fstatus,
                             "climate": cl, "cstatus": cstatus}}
        cycles = [{"id": cyc.id, "property_id": prop.id, "crop_name": "Buza",
                   "sow_date": cyc.sow_date,
                   "expected_end_date": cyc.expected_end_date}]
        items, _ = generate_plan(cycles, [], weather=weather)
        text = " ".join(items[0]["weather_annotations"])
        self.assertIn("hiányzik", text.lower())

    def test_plans_get_shows_cached_weather_no_network(self):
        from django.contrib.auth import get_user_model as _GU
        from datetime import date as _date
        from unittest.mock import patch as _patch
        from portal.models import CropCycle as _CC, Farm as _F, FarmLocation as _FL, FarmProperty as _FP
        u = _GU().objects.create_user(email="pv@example.com", password="TesztJelszo123!")
        f = _F.objects.create(owner=u, name="FV")
        loc = _FL.objects.create(farm=f, name="LV", latitude="46.250000",
                                 longitude="20.150000", timezone="Europe/Budapest")
        prop = _FP.objects.create(farm=f, location=loc, name="PV", kind="field",
                                  area_hectares="1")
        _CC.objects.create(farm=f, property=prop, crop_name="Buza", state="current",
                           sow_date=_date(2026, 3, 1), expected_end_date=_date(2026, 7, 1))
        tr = _transport_for("both")
        W.refresh_forecast(f.id, loc, transport=tr)
        W.refresh_climate(f.id, loc, years=1, transport=tr)
        self.client.force_login(u)
        with _patch("portal.weather.httpx.get",
                    side_effect=AssertionError("plans GET must not hit network")):
            r = self.client.post("/terv/")
            self.assertIn(r.status_code, (302, 200))
            r = self.client.get("/terv/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "NOT forecast")
        self.assertContains(r, "mm")
