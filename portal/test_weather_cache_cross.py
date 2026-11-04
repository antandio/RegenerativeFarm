"""Shared weather cache across gunicorn workers (TDD).

Default LocMemCache is per-process: with 2 gunicorn workers a refresh on one
worker is invisible to the other. Production therefore uses a private
FileBasedCache (BASE_DIR/'weather_cache', mode 0700); the test suite keeps
using LocMemCache automatically (see config/settings.py).

These tests prove, with two *separate* FileBasedCache instances sharing one
temp dir (simulating two workers), that refresh-then-get is shared, and that
the cache key binds farm id + coordinates + timezone.
"""
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from django.conf import settings
from django.test import TestCase
from django.contrib.auth import get_user_model

from portal import weather as W
from portal.models import Farm, FarmLocation

User = get_user_model()

WORKLOGS = Path(tempfile.gettempdir())


def _mkuser(email="wx@example.com"):
    return User.objects.create_user(email=email, password="HosszuJelszo123")


def _farm_loc(owner, lat="46.250000", lon="20.150000", tz="Europe/Budapest"):
    f = Farm.objects.create(owner=owner, name="F")
    loc = FarmLocation.objects.create(
        farm=f, name="H", latitude=lat, longitude=lon, timezone=tz)
    return f, loc


def _forecast_body():
    return {"current": {"time": "2026-01-01T12:00", "temperature_2m": 5.0,
                        "precipitation": 0.2, "wind_speed_10m": 12.0},
            "current_units": {"temperature_2m": "°C", "precipitation": "mm",
                              "wind_speed_10m": "km/h"},
            "daily": {"time": ["2026-01-0%d" % i for i in range(1, 8)],
                      "temperature_2m_max": [6.0] * 7,
                      "temperature_2m_min": [-1.0] * 7,
                      "precipitation_sum": [0.5] * 7,
                      "wind_speed_10m_max": [15.0] * 7}}


def _transport():
    def h(req):
        return httpx.Response(200, json=_forecast_body())
    return httpx.MockTransport(h)


def _two_instances(testcase):
    """Two separate FileBasedCache objects, one shared temp dir under worklogs."""
    from django.core.cache.backends.filebased import FileBasedCache
    parent = WORKLOGS if WORKLOGS.is_dir() else None
    tmp = tempfile.mkdtemp(prefix="weather-cache-proof-",
                           dir=str(parent) if parent else None)
    os.chmod(tmp, 0o700)
    testcase.addCleanup(shutil.rmtree, tmp, True)
    params = {"TIMEOUT": 300, "MAX_ENTRIES": 1000}
    return tmp, FileBasedCache(tmp, params), FileBasedCache(tmp, params)


class WeatherCacheSettingsTests(TestCase):
    def test_suite_uses_locmem_automatically(self):
        backend = settings.CACHES["default"]["BACKEND"]
        self.assertIn("LocMem", backend)

    def test_production_settings_declare_private_filebasedcache(self):
        src = (Path(settings.BASE_DIR) / "config" / "settings.py").read_text()
        self.assertIn("FileBasedCache", src)
        self.assertIn("weather_cache", src)
        self.assertIn("test", src)  # 'test' in sys.argv guard
        self.assertIn("0o700", src)
        self.assertIn("symlink", src.lower())
        gitignore = (Path(settings.BASE_DIR) / ".gitignore").read_text()
        self.assertIn("weather_cache/", gitignore)


class WeatherCacheCrossInstanceTests(TestCase):
    def test_refresh_on_one_instance_visible_on_other(self):
        u = _mkuser()
        f, loc = _farm_loc(u)
        tmp, cache_a, cache_b = _two_instances(self)
        with patch.object(W, "cache", cache_a):
            payload, status = W.refresh_forecast(f.id, loc, transport=_transport())
        self.assertEqual(status, "fresh")
        self.assertIsNotNone(payload)
        # Simulate a *different* worker process: fresh object, same dir.
        # A plain set on A is already visible on B (proven below); here the
        # full refresh path on A must leave a record the sibling can read.
        raw = cache_b.get(W._ck("forecast", f.id, loc))
        self.assertIsInstance(raw, dict)
        self.assertIn("payload", raw)
        self.assertEqual(raw["payload"]["current"], payload["current"])
        self.assertEqual(len(raw["payload"]["daily"]), 7)
        # Explicit wrapper path on the sibling instance (not raw get only):
        # the production read path must see the refresh as fresh.
        with patch.object(W, "cache", cache_b):
            got, status = W.get_forecast_cached(f.id, loc)
        self.assertEqual(status, "fresh")
        self.assertIsNotNone(got)
        self.assertEqual(got["current"], payload["current"])
        self.assertEqual(len(got["daily"]), 7)
        # Proof files really landed in the shared dir (not in-process memory).
        self.assertTrue([p for p in os.listdir(tmp) if p.endswith(".djcache")])

    def test_key_binds_farm_lat_lon_tz(self):
        u = _mkuser()
        f, loc = _farm_loc(u)
        key = W._ck("forecast", f.id, loc)
        self.assertIn(str(f.id), key)
        self.assertIn("46.25", key)
        self.assertIn("20.15", key)
        self.assertIn("Europe/Budapest", key)

    def test_distinct_coords_distinct_keys(self):
        u = _mkuser()
        f, loc = _farm_loc(u)
        tmp, cache_a, cache_b = _two_instances(self)
        with patch.object(W, "cache", cache_a):
            W.refresh_forecast(f.id, loc, transport=_transport())
        loc.latitude = "47.000000"
        loc.save()
        with patch.object(W, "cache", cache_b):
            got, status = W.get_forecast_cached(f.id, loc)
        self.assertEqual(status, "missing")
        self.assertIsNone(got)
