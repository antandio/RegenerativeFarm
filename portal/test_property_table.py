"""Task 4 RED: modern semantic properties table (property branch only).

Strict assertions: table/caption/scope/rows/actions/history/new/empty/
readonly-tenant/HU-EN + overview map retained above table + location branch
(Task 3 table) and equipment/crop branches (ul.entity-list) preserved.
"""
import re
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from .models import Farm, Membership
from .domain_models import FarmLocation, FarmProperty

User = get_user_model()
PW = "TesztJelszo123!"


def sq(lon0=20.0, lat0=46.0, s=0.01):
    ring = [[lon0, lat0], [lon0 + s, lat0], [lon0 + s, lat0 + s],
            [lon0, lat0 + s], [lon0, lat0]]
    return {"type": "Polygon", "coordinates": [ring]}


def make_farm(email="prop4owner@pelda.hu", name="P4 Farm"):
    u = User.objects.create_user(email=email, password=PW)
    farm = Farm.objects.create(owner=u, name=name)
    c = Client()
    c.force_login(u)
    return u, farm, c


def make_member(farm, email, role):
    u = User.objects.create_user(email=email, password=PW)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    c = Client()
    c.force_login(u)
    return u, c


def add_loc(farm, name="Tanya4", lat="46.250000", lon="20.140000",
            tz="Europe/Budapest"):
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude=lat, longitude=lon, timezone=tz)


def add_prop(farm, loc, name="Azonosithato-Parcella-4",
             kind="field", area="5.00", geo=None):
    return FarmProperty.objects.create(
        farm=farm, location=loc, name=name, kind=kind,
        area_hectares=Decimal(area), boundary_geojson=geo)


class PropertyTableTests(TestCase):
    def test_property_branch_is_semantic_table(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        add_prop(farm, loc)
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('<table class="domain-table domain-table--properties"', b)
        self.assertIn('<div class="table-scroll"', b)
        self.assertIn("<caption", b)
        self.assertIn('scope="col"', b)
        self.assertIn('scope="row"', b)
        for header in ("Név", "Típus", "Helyszín", "Terület (ha)",
                       "Becsült térképi terület (ha)", "nem kataszteri",
                       "Műveletek"):
            self.assertIn(header, b)

    def test_rows_values_actions_history_and_new(self):
        _, farm, c = make_farm()
        loc = add_loc(farm, name="Tanya-Hely-4")
        o = add_prop(farm, loc, name="Parcella-4-Azonosithato", geo=sq())
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn("Parcella-4-Azonosithato", b)
        self.assertIn(o.get_kind_display(), b)
        self.assertIn("Tanya-Hely-4", b)
        self.assertTrue("5.00" in b or "5,00" in b, b[:3000])
        self.assertIn('id="property-%s"' % o.pk, b)
        self.assertIn(reverse("event_list") + "?property=%s" % o.pk, b)
        self.assertIn("Előzmények", b)
        self.assertIn(reverse("property_edit", kwargs={"pk": o.pk}), b)
        self.assertIn(reverse("property_delete", kwargs={"pk": o.pk}), b)
        self.assertIn(reverse("property_new"), b)

    def test_declared_and_estimated_areas_are_separate_cells(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        o = add_prop(farm, loc, geo=sq())
        b = c.get(reverse("property_list")).content.decode()
        m = re.search(r'<tr id="property-%s">(.*?)</tr>' % o.pk, b, re.S)
        self.assertIsNotNone(m, "missing property row")
        row = m.group(1)
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        self.assertGreaterEqual(len(cells), 6, row[:1000])
        declared = [x for x in cells if "5" in x]
        self.assertTrue(declared, row[:1000])
        self.assertNotIn("kataszteri", row)
        self.assertIn("kataszteri", b)
        est = o.map_area_hectares
        self.assertIsNotNone(est)
        self.assertRegex(row, r"\d+[,.]\d{2}")

    def test_estimated_missing_renders_dash_not_mixed(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        o = add_prop(farm, loc, name="Terulet-Becsles-Nelkul-4", geo=None,
                     area="3.00")
        b = c.get(reverse("property_list")).content.decode()
        m = re.search(r'<tr id="property-%s">(.*?)</tr>' % o.pk, b, re.S)
        self.assertIsNotNone(m, "missing property row")
        row = m.group(1)
        self.assertIn("—", row)
        self.assertTrue("3.00" in row or "3,00" in row, row[:1000])

    def test_empty_state_colspan(self):
        _, _, c = make_farm(email="prop4empty@pelda.hu")
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('<table class="domain-table domain-table--properties"', b)
        self.assertIn("Nincs elem.", b)
        self.assertIn('colspan="6"', b)

    def test_readonly_viewer_history_and_map_kept_no_write(self):
        _, farm, _ = make_farm()
        loc = add_loc(farm)
        o = add_prop(farm, loc, geo=sq())
        _, vc = make_member(farm, "prop4viewer@pelda.hu", "viewer")
        b = vc.get(reverse("property_list")).content.decode()
        self.assertIn('<table class="domain-table domain-table--properties"', b)
        self.assertIn("Azonosithato-Parcella-4", b)
        self.assertIn('id="property-overview-map"', b)
        self.assertIn(reverse("event_list") + "?property=%s" % o.pk, b)
        self.assertNotIn(reverse("property_new"), b)
        self.assertNotIn("property_edit", b)
        self.assertNotIn("property_delete", b)

    def test_tenant_isolation_no_leak(self):
        _, farm, c = make_farm()
        other_u = User.objects.create_user(
            email="prop4other@pelda.hu", password=PW)
        other_farm = Farm.objects.create(owner=other_u, name="Masik Farm")
        other_loc = add_loc(other_farm, name="MASIK-HELYSZIN-4")
        add_prop(other_farm, other_loc, name="MASIK-FARM-TITKOS-TERULET-4")
        loc = add_loc(farm)
        add_prop(farm, loc, name="SAJAT-TERULET-4")
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn("SAJAT-TERULET-4", b)
        self.assertNotIn("MASIK-FARM-TITKOS-TERULET-4", b)
        self.assertNotIn("MASIK-HELYSZIN-4", b)

    def test_english_renders(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        o = add_prop(farm, loc, geo=sq())
        c.post(reverse("set_language"), {"language": "en"})
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('<table class="domain-table domain-table--properties"', b)
        for header in ("Name", "Type", "Location", "Area (ha)",
                       "Estimated map area", "not cadastral", "Actions",
                       "History"):
            self.assertIn(header, b)
        self.assertIn('id="property-%s"' % o.pk, b)

    def test_map_retained_above_table(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        add_prop(farm, loc, name="Alpha4", geo=sq())
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('id="property-overview-map"', b)
        self.assertIn('id="property-overview-data"', b)
        self.assertIn("OpenStreetMap", b)
        self.assertIn("fitBounds", b)
        self.assertIn("vendor/leaflet-1.9.4/leaflet.js", b)
        self.assertLess(b.index("property-overview-map"),
                        b.index("domain-table"))

    def test_other_branches_preserved_exact(self):
        _, _, c = make_farm()
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        # Task 5 delivered: equipment branch is now a domain-table too.
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        b = c.get(reverse("cycle_list")).content.decode()
        self.assertIn('<ul class="entity-list">', b)
        self.assertNotIn("domain-table", b)


class PropertyTableMobileScrollTests(TestCase):
    """Task4 mobile390: hectares must not split, actions stay reachable."""

    def test_scroll_container_keyboard_accessible(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        add_prop(farm, loc)
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn(
            '<div class="table-scroll" tabindex="0" role="region" aria-label=', b)

    def test_numeric_cells_nowrap_and_minwidth(self):
        import pathlib
        css = (pathlib.Path(__file__).parent
               / "static/portal/app.css").read_text()
        m = re.search(r"\.domain-table\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing .domain-table rule")
        self.assertRegex(m.group(1), r"min-width:\s*640px")
        mm = re.search(r"\.domain-table\s+\.coord\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(mm, "missing .coord rule")
        self.assertRegex(mm.group(1), r"white-space:\s*nowrap")
        wrap = re.search(
            r"\.table-scroll\s*\{([^}]*)\}", css, re.S)
        self.assertIsNotNone(wrap, "missing .table-scroll rule")
        self.assertRegex(wrap.group(1), r"overflow-x:\s*auto")
        self.assertRegex(wrap.group(1), r"width:\s*100%")
        _, farm, c = make_farm(email="prop4css@pelda.hu")
        loc = add_loc(farm)
        add_prop(farm, loc)
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('class="coord"', b)

    def test_scroll_hint_visible(self):
        _, farm, c = make_farm(email="prop4hint@pelda.hu")
        loc = add_loc(farm)
        add_prop(farm, loc)
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn("scroll-hint", b)
