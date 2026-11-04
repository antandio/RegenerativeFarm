"""Task 3 RED: modern semantic locations table (location branch only).

Strict assertions: table/caption/scope/rows/actions/new/empty/readonly/HU-EN
+ property/equipment/crop branches preserved EXACT (ul.entity-list).
"""
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from .models import Farm, Membership
from .domain_models import FarmLocation

User = get_user_model()
PW = "TesztJelszo123!"


def make_farm(email="loc3owner@pelda.hu", name="L3 Farm"):
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


def add_loc(farm, name="Szeged kozpont", lat="46.253000", lon="20.141000",
            tz="Europe/Budapest"):
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude=lat, longitude=lon, timezone=tz)


class LocationTableTests(TestCase):
    def test_location_branch_is_semantic_table(self):
        _, farm, c = make_farm()
        add_loc(farm)
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn('<div class="table-scroll"', b)
        self.assertIn("<caption", b)
        self.assertIn('scope="col"', b)
        self.assertIn('scope="row"', b)

    def test_rows_values_and_actions_and_new(self):
        _, farm, c = make_farm()
        o = add_loc(farm, name="Tanya-01-Azonosithato")
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn("Tanya-01-Azonosithato", b)
        self.assertIn("46,253", b)
        self.assertIn("20,141", b)
        self.assertIn("Europe/Budapest", b)
        self.assertIn(reverse("location_edit", kwargs={"pk": o.pk}), b)
        self.assertIn(reverse("location_delete", kwargs={"pk": o.pk}), b)
        self.assertIn(reverse("location_new"), b)

    def test_empty_state_colspan(self):
        _, _, c = make_farm(email="loc3empty@pelda.hu")
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn("Nincs elem.", b)
        self.assertRegex(b, r'colspan="[45]"')

    def test_readonly_viewer_no_actions_no_new(self):
        _, farm, _ = make_farm()
        add_loc(farm)
        _, vc = make_member(farm, "loc3viewer@pelda.hu", "viewer")
        b = vc.get(reverse("location_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn("Szeged kozpont", b)
        self.assertNotIn(reverse("location_new"), b)
        self.assertNotIn("location_edit", b)
        self.assertNotIn("location_delete", b)
        self.assertNotIn("Műveletek", b)
        self.assertNotIn("Actions", b)

    def test_tenant_isolation_no_leak(self):
        _, farm, c = make_farm()
        other_u = User.objects.create_user(
            email="loc3other@pelda.hu", password=PW)
        other_farm = Farm.objects.create(owner=other_u, name="Masik Farm")
        add_loc(other_farm, name="MASIK-FARM-TITKOS-HELYSZIN")
        add_loc(farm, name="SAJAT-HELYSZIN")
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn("SAJAT-HELYSZIN", b)
        self.assertNotIn("MASIK-FARM-TITKOS-HELYSZIN", b)

    def test_english_renders(self):
        _, farm, c = make_farm()
        add_loc(farm)
        b = c.get(reverse("location_list"),
                  HTTP_ACCEPT_LANGUAGE="en").content.decode()
        # force EN via session lang switch
        c.post(reverse("set_language"), {"language": "en"})
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn("Europe/Budapest", b)

    def test_other_branches_preserved_exact(self):
        # Task 4 delivered: property branch is now a domain-table too.
        # Task 5 delivered: equipment branch is now a domain-table too.
        _, _, c = make_farm()
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('<table class="domain-table domain-table--properties"', b)
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        b = c.get(reverse("cycle_list")).content.decode()
        self.assertIn('<ul class="entity-list">', b)
        self.assertNotIn("domain-table", b)


class LocationTableMobileScrollTests(TestCase):
    """Task3 mobile390: coords/tz must not split, actions stay reachable."""

    def test_scroll_container_keyboard_accessible(self):
        _, farm, c = make_farm()
        add_loc(farm)
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn(
            '<div class="table-scroll" tabindex="0" role="region" aria-label=', b)

    def test_numeric_cells_nowrap_and_minwidth(self):
        import pathlib
        import re
        css = (pathlib.Path(__file__).parent
               / "static/portal/app.css").read_text()
        m = re.search(r"\.domain-table\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing .domain-table rule")
        self.assertRegex(m.group(1), r"min-width:\s*640px")
        for sel in (r"\.domain-table\s+\.coord\s*\{(.*?)\}",
                    r"\.domain-table\s+\.tz\s*\{(.*?)\}"):
            mm = re.search(sel, css, re.S)
            self.assertIsNotNone(mm, f"missing rule {sel}")
            self.assertRegex(mm.group(1), r"white-space:\s*nowrap")
        wrap = re.search(
            r"\.table-scroll\s*\{([^}]*)\}", css, re.S)
        self.assertIsNotNone(wrap, "missing .table-scroll rule")
        self.assertRegex(wrap.group(1), r"overflow-x:\s*auto")
        self.assertRegex(wrap.group(1), r"width:\s*100%")

    def test_scroll_hint_visible(self):
        _, farm, c = make_farm()
        add_loc(farm)
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn("scroll-hint", b)
