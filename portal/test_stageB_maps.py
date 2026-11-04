"""Stage B map UI tracer — RED-first.

Covers: template compilation, local vendor assets (no runtime CDN),
role-scoped serialization/XSS (json_script + textContent popups),
invalid-POST map data preserved, form roundtrip clearing.

Run:
  ./.venv/bin/python manage.py test portal.test_stageB_maps
"""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.template import engines
from django.test import Client, TestCase
from django.urls import reverse

from portal.domain_models import FarmLocation, FarmProperty
from portal.models import Farm, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def sq(lon0=20.0, lat0=46.0, s=0.01):
    ring = [[lon0, lat0], [lon0 + s, lat0], [lon0 + s, lat0 + s],
            [lon0, lat0 + s], [lon0, lat0]]
    return {"type": "Polygon", "coordinates": [ring]}


def make_owner(email="mapowner@example.com"):
    u = User.objects.create_user(email=email, password=PW)
    f = Farm.objects.create(owner=u, name="Map Farm")
    Membership.objects.create(farm=f, user=u, role="owner", is_active=True)
    c = Client()
    c.force_login(u)
    return u, f, c


def make_member(farm, email, role):
    u = User.objects.create_user(email=email, password=PW)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    c = Client()
    c.force_login(u)
    return u, c


def make_loc_prop(farm, name="P1", geo=None, lat=None, lon=None):
    loc = FarmLocation.objects.create(farm=farm, name="L-" + name,
                                      latitude=Decimal("46.25"),
                                      longitude=Decimal("20.14"),
                                      timezone="Europe/Budapest")
    p = FarmProperty.objects.create(farm=farm, location=loc, name=name,
                                    kind="field", area_hectares=Decimal("5.00"),
                                    boundary_geojson=geo,
                                    point_latitude=lat, point_longitude=lon)
    return loc, p


class TemplateCompilationTests(TestCase):
    def test_property_form_template_compiles(self):
        engines["django"].get_template("portal/property_form.html")

    def test_overview_fragment_compiles(self):
        engines["django"].get_template("portal/_property_overview_map.html")


class VendorAssetTests(TestCase):
    def test_no_runtime_cdn_in_map_templates(self):
        for tpl in ("portal/property_form.html", "portal/_property_overview_map.html"):
            src = engines["django"].get_template(tpl).template.source
            for cdn in ("unpkg.com", "cdnjs", "cdn.jsdelivr", "jsdelivr"):
                self.assertNotIn(cdn, src, tpl)

    def test_vendored_files_present(self):
        import pathlib
        base = pathlib.Path(__file__).parent / "static" / "portal" / "vendor"
        for rel in ("leaflet-1.9.4/leaflet.js", "leaflet-1.9.4/leaflet.css",
                    "leaflet-draw-1.0.4/leaflet.draw.js",
                    "leaflet-draw-1.0.4/leaflet.draw.css",
                    "leaflet-draw-1.0.4/images/spritesheet.png",
                    "leaflet-draw-1.0.4/images/spritesheet-2x.png",
                    "leaflet-draw-1.0.4/images/spritesheet.svg",
                    "VENDOR_MAP_LIBS.txt"):
            self.assertTrue((base / rel).exists(), rel)


class PropertyFormMapTests(TestCase):
    def test_new_uses_dedicated_template_with_map(self):
        _, _, c = make_owner()
        r = c.get(reverse("property_new"))
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "portal/property_form.html")
        b = r.content.decode()
        self.assertIn('id="property-map"', b)
        self.assertIn("vendor/leaflet-1.9.4/leaflet.js", b)
        self.assertIn("vendor/leaflet-draw-1.0.4/leaflet.draw.js", b)
        self.assertIn("OpenStreetMap", b)  # tile attribution
        self.assertIn("json", b)
        # fallback fields always visible
        self.assertIn('name="boundary_geojson"', b)
        self.assertIn('name="point_latitude"', b)
        self.assertIn('name="point_longitude"', b)

    def test_edit_renders_existing_geometry(self):
        _, f, c = make_owner()
        loc, p = make_loc_prop(f, geo=sq(), lat=Decimal("46.25"), lon=Decimal("20.14"))
        r = c.get(reverse("property_edit", kwargs={"pk": p.pk}))
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "portal/property_form.html")
        b = r.content.decode()
        self.assertIn('id="property-map"', b)
        self.assertIn("Polygon", b)

    def test_invalid_post_preserves_map_data(self):
        """Invalid POST must not discard the whole map payload on rerender."""
        _, f, c = make_owner()
        loc = FarmLocation.objects.create(farm=f, name="Lx", latitude=Decimal("46.25"),
                                          longitude=Decimal("20.14"),
                                          timezone="Europe/Budapest")
        payload = {"location": str(loc.pk), "name": "",  # invalid: empty name
                   "kind": "field", "area_hectares": "3.00", "notes": "",
                   "boundary_geojson": json.dumps(sq()),
                   "point_latitude": "46.25", "point_longitude": "20.14"}
        r = c.post(reverse("property_new"), payload)
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Polygon", b)  # polygon JSON still present
        self.assertIn("46.25", b)  # point fields still present
        self.assertIn('id="property-map"', b)

    def test_form_roundtrip_clearing(self):
        _, f, c = make_owner()
        loc, p = make_loc_prop(f, geo=sq(), lat=Decimal("46.25"), lon=Decimal("20.14"))
        r = c.post(reverse("property_edit", kwargs={"pk": p.pk}),
                   {"location": str(loc.pk), "name": p.name, "kind": "field",
                    "area_hectares": "5.00", "notes": "",
                    "boundary_geojson": "", "point_latitude": "", "point_longitude": ""})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertIsNone(p.boundary_geojson)
        self.assertIsNone(p.point_latitude)
        self.assertIsNone(p.point_longitude)
        self.assertIsNone(p.map_area_hectares)

    def test_point_never_updates_shared_center(self):
        _, f, c = make_owner()
        loc, p = make_loc_prop(f)
        c.post(reverse("property_new"),
               {"location": str(loc.pk), "name": "Q", "kind": "field",
                "area_hectares": "1.00", "notes": "",
                "boundary_geojson": "",
                "point_latitude": "47.00", "point_longitude": "21.00"})
        loc.refresh_from_db()
        self.assertEqual(str(loc.latitude), "46.250000")
        self.assertEqual(str(loc.longitude), "20.140000")


class OverviewMapTests(TestCase):
    def test_list_has_overview_map_and_fallback(self):
        _, f, c = make_owner()
        make_loc_prop(f, name="Alpha", geo=sq())
        make_loc_prop(f, name="Beta", geo=None, lat=Decimal("46.5"), lon=Decimal("20.5"))
        r = c.get(reverse("property_list"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn('id="property-overview-map"', b)
        self.assertIn("Alpha", b)
        self.assertIn("Beta", b)
        self.assertIn("OpenStreetMap", b)
        self.assertIn("fitBounds", b)

    def test_xss_name_safely_serialized(self):
        _, f, c = make_owner()
        evil = '</script><script>alert("x")</script>'
        make_loc_prop(f, name=evil, geo=sq())
        r = c.get(reverse("property_list"))
        b = r.content.decode()
        self.assertNotIn("</script><script>alert", b)
        self.assertIn("lgettext", b.lower() if False else b) if False else None
        # json_script escaping: < must be \u003c
        self.assertIn("\\u003c/script\\u003e", b)

    def test_role_scoped_serialization(self):
        u, f, c = make_owner()
        make_loc_prop(f, name="Mine", geo=sq())
        other_owner = User.objects.create_user(email="other@example.com", password=PW)
        of = Farm.objects.create(owner=other_owner, name="Other")
        Membership.objects.create(farm=of, user=other_owner, role="owner", is_active=True)
        oloc = FarmLocation.objects.create(farm=of, name="OL", latitude=Decimal("47"),
                                           longitude=Decimal("21"),
                                           timezone="Europe/Budapest")
        FarmProperty.objects.create(farm=of, location=oloc, name="NotMine",
                                    kind="field", area_hectares=Decimal("1"))
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn("Mine", b)
        self.assertNotIn("NotMine", b)

    def test_worker_readonly_no_edit_links(self):
        _, f, _ = make_owner(email="own2@example.com")
        _, wc = make_member(f, "worker@example.com", "worker")
        make_loc_prop(f, name="W1", geo=sq())
        r = wc.get(reverse("property_list"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn('id="property-overview-map"', b)
        self.assertNotIn("property_edit", b)
        # worker POST blocked
        loc = FarmLocation.objects.filter(farm=f).first()
        r2 = wc.post(reverse("property_new"),
                     {"location": str(loc.pk), "name": "No", "kind": "field",
                      "area_hectares": "1.00", "notes": "",
                      "boundary_geojson": "", "point_latitude": "", "point_longitude": ""})
        self.assertEqual(r2.status_code, 403)
