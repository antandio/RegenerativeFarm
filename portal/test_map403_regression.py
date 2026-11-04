"""Map 403 regression tests — RED-first.

Confirmed integration defect (independently verified with real deployed
Chromium requests, NOT reproduced as a 403 locally):
- Django responses carry ``Referrer-Policy: same-origin`` and both Leaflet
  tile layers leave the tile image ``referrerPolicy`` empty, so all outgoing
  tile requests omit ``Referer``.
- The official OSM tile policy
  (https://operations.osmfoundation.org/policies/tiles/) explicitly requires
  a ``Referer`` and disallows suppressing it.

NOTE on causality: the remote browser currently still receives valid HTTP 200
PNGs despite the missing ``Referer``, so the user's actual 403 is NOT
reproduced and proven causality is NOT claimed. This file pins the confirmed
defect only: missing origin-only Referer on tile requests.

Scope (narrow by design):
- ``referrerPolicy: 'strict-origin-when-cross-origin'`` on BOTH tile layers
  (origin only, no full-URL leak; no page-header relaxation, no provider or
  domain change, no identity spoofing, no retries, no-cache or proxying).
- Proper linked OSM copyright attribution on both layers.
- Visible localized HU/EN ``tileerror`` status guidance on both maps; manual
  geometry fields stay usable; one later ``tileload`` must NOT silently reset
  the warning (no ``tileload`` status reset at all).
- Cache-busted ``?v=`` query on both app ``<script>`` tags so browsers with
  the old cached file upgrade.
- No gettext catalog churn: HU/EN strings live in JS, detected from
  ``document.documentElement.lang``.

Environment note: no Node/JS runtime exists in this repo container, so these
executable regression tests observe the ACTUAL shipped script files
(``portal/static/portal/*.js``) plus Django-rendered templates, instead of a
Node fake-Leaflet/DOM harness. The assertions below read the real files.

Run (focused only; full suite is run separately by the caller):
  ./.venv/bin/python manage.py test portal.test_map403_regression
"""
import json
import pathlib
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from portal.domain_models import FarmLocation, FarmProperty
from portal.models import Farm, Membership

User = get_user_model()
PW = "TesztJelszo123!"

STATIC_DIR = pathlib.Path(__file__).parent / "static" / "portal"


def read_js(name):
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def sq(lon0=20.0, lat0=46.0, s=0.01):
    ring = [[lon0, lat0], [lon0 + s, lat0], [lon0 + s, lat0 + s],
            [lon0, lat0 + s], [lon0, lat0]]
    return {"type": "Polygon", "coordinates": [ring]}


def make_owner(email="map403owner@example.com"):
    u = User.objects.create_user(email=email, password=PW)
    f = Farm.objects.create(owner=u, name="Map403 Farm")
    Membership.objects.create(farm=f, user=u, role="owner", is_active=True)
    c = Client()
    c.force_login(u)
    return u, f, c


FORM_JS = "property_form_map.js"
OVERVIEW_JS = "property_overview_map.js"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


class TileReferrerPolicyTests(TestCase):
    """Both tile layers must send origin-only Referer (no page-header change)."""

    def test_form_tile_layer_sends_origin_referrer(self):
        js = read_js(FORM_JS)
        self.assertIn(TILE_URL, js)  # provider/domain unchanged
        self.assertIn("referrerPolicy", js)
        self.assertIn("strict-origin-when-cross-origin", js)

    def test_overview_tile_layer_sends_origin_referrer(self):
        js = read_js(OVERVIEW_JS)
        self.assertIn(TILE_URL, js)  # provider/domain unchanged
        self.assertIn("referrerPolicy", js)
        self.assertIn("strict-origin-when-cross-origin", js)

    def test_no_permissive_page_referrer_relaxation(self):
        """Guard: fix must not relax the page Referrer-Policy header."""
        _, _, c = make_owner()
        r = c.get(reverse("property_new"))
        policy = r.headers.get("Referrer-Policy", "")
        self.assertNotIn("no-referrer", policy)
        self.assertNotIn("unsafe-url", policy)
        self.assertNotIn("origin-when-cross-origin", policy.replace("strict-", ""))


class TileAttributionTests(TestCase):
    """Both layers need proper linked OSM copyright attribution."""

    def test_form_linked_attribution(self):
        js = read_js(FORM_JS)
        self.assertIn("OpenStreetMap", js)
        self.assertIn("openstreetmap.org/copyright", js)
        self.assertIn("<a ", js)

    def test_overview_linked_attribution(self):
        js = read_js(OVERVIEW_JS)
        self.assertIn("OpenStreetMap", js)
        self.assertIn("openstreetmap.org/copyright", js)
        self.assertIn("<a ", js)


class TileErrorGuidanceTests(TestCase):
    """Visible localized tile-failure guidance; manual fields stay usable."""

    def test_form_tileerror_status_hu_en(self):
        js = read_js(FORM_JS)
        self.assertIn("tileerror", js)
        self.assertIn("property-map-tiles-status", js)
        # Localized guidance (JS-side, no gettext churn).
        self.assertIn("csempéi nem tölthetők be", js)  # HU
        self.assertIn("fill in the fields below manually", js)  # EN

    def test_overview_tileerror_status_hu_en(self):
        js = read_js(OVERVIEW_JS)
        self.assertIn("tileerror", js)
        self.assertIn("property-overview-tiles-status", js)
        self.assertIn("csempéi nem tölthetők be", js)  # HU
        self.assertIn("The list below is complete", js)  # EN

    def test_no_silent_reset_on_tileload(self):
        """One later tileload must NOT silently clear the warning."""
        for name in (FORM_JS, OVERVIEW_JS):
            js = read_js(name)
            # No tileload event handler may touch the sticky warning
            # (mentions inside comments/strings are fine).
            self.assertNotRegex(js, r"""\.on\(\s*["']tileload["']""", name)


class TemplateCacheBustTests(TestCase):
    """Both app script tags cache-busted; status elements rendered."""

    def test_form_template_status_and_cachebust(self):
        _, _, c = make_owner()
        b = c.get(reverse("property_new")).content.decode()
        self.assertIn('id="property-map-tiles-status"', b)
        self.assertIn("property_form_map.js?v=", b)
        # Manual geometry fields remain usable.
        self.assertIn('name="boundary_geojson"', b)
        self.assertIn('name="point_latitude"', b)
        self.assertIn('name="point_longitude"', b)

    def test_overview_template_status_and_cachebust(self):
        _, f, c = make_owner()
        loc = FarmLocation.objects.create(
            farm=f, name="L1", latitude=Decimal("46.25"),
            longitude=Decimal("20.14"), timezone="Europe/Budapest")
        FarmProperty.objects.create(
            farm=f, location=loc, name="P1", kind="field",
            area_hectares=Decimal("5.00"), boundary_geojson=sq())
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('id="property-overview-map"', b)
        self.assertIn('id="property-overview-tiles-status"', b)
        self.assertIn("property_overview_map.js?v=", b)
        self.assertIn("P1", b)  # list fallback below the map stays complete


class GeometryPreservationTests(TestCase):
    """Pre-existing data/geometry behavior must survive the fix."""

    def test_form_map_data_and_stale_warning_intact(self):
        _, _, c = make_owner()
        b = c.get(reverse("property_new")).content.decode()
        self.assertIn('id="property-map"', b)
        self.assertIn("property-map-data", b)
        self.assertIn("property-map-stale", b)
        js = read_js(FORM_JS)
        self.assertIn("fitBounds", js)
        self.assertIn("syncFromLayers", js)

    def test_overview_fitbounds_and_popups_intact(self):
        js = read_js(OVERVIEW_JS)
        self.assertIn("fitBounds", js)
        self.assertIn("textContent", js)

    def test_invalid_post_preserves_map_data_with_status(self):
        _, f, c = make_owner()
        loc = FarmLocation.objects.create(
            farm=f, name="Lx", latitude=Decimal("46.25"),
            longitude=Decimal("20.14"), timezone="Europe/Budapest")
        payload = {"location": str(loc.pk), "name": "",  # invalid: empty name
                   "kind": "field", "area_hectares": "3.00", "notes": "",
                   "boundary_geojson": json.dumps(sq()),
                   "point_latitude": "46.25", "point_longitude": "20.14"}
        r = c.post(reverse("property_new"), payload)
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Polygon", b)
        self.assertIn("46.25", b)
        self.assertIn('id="property-map"', b)
        self.assertIn('id="property-map-tiles-status"', b)
