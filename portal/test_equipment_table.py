"""Task 5 RED: modern semantic equipment table (equipment branch only).

Strict assertions: table/caption/scope/rows/actions/new/empty/
readonly-tenant/HU-EN + location/property branches (Task 3/4 tables)
and crop branch (ul.entity-list) preserved. Capability values stay
truthful to the stored list join; status is accessible text (not
color-only); all M2M property names shown, empty -> dash.
"""
import re
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from .models import Farm, Membership
from .domain_models import Equipment, EquipmentProperty, FarmLocation, FarmProperty

User = get_user_model()
PW = "TesztJelszo123!"


def make_farm(email="eq5owner@pelda.hu", name="E5 Farm"):
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


def add_loc(farm, name="Tanya5", lat="46.250000", lon="20.140000",
            tz="Europe/Budapest"):
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude=lat, longitude=lon, timezone=tz)


def add_prop(farm, loc, name="Parcella-5"):
    return FarmProperty.objects.create(
        farm=farm, location=loc, name=name, kind="field",
        area_hectares=Decimal("5.00"))


def add_eq(farm, name="Traktor-5-Azonosithato", caps=None,
           available=True, hours="6.5", props=()):
    o = Equipment.objects.create(
        farm=farm, name=name,
        capabilities=list(caps) if caps is not None else ["tillage"],
        available=available, hours_per_day=Decimal(hours))
    for p in props:
        EquipmentProperty.objects.create(equipment=o, property=p)
    return o


class EquipmentTableTests(TestCase):
    def test_equipment_branch_is_semantic_table(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        prop = add_prop(farm, loc)
        add_eq(farm, props=[prop])
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn('<div class="table-scroll"', b)
        self.assertIn("<caption", b)
        self.assertIn('scope="col"', b)
        self.assertIn('scope="row"', b)
        for header in ("Név", "Képességek", "Elérhetőség",
                       "Napi óraszám", "Területek", "Műveletek"):
            self.assertIn(header, b)

    def test_rows_values_actions_and_new(self):
        _, farm, c = make_farm()
        loc = add_loc(farm, name="Tanya-Hely-5")
        prop = add_prop(farm, loc, name="Parcella-Hely-5")
        o = add_eq(farm, name="Gep-5-Azonosithato",
                   caps=["tillage", "transport"], available=True,
                   hours="6.5", props=[prop])
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn("Gep-5-Azonosithato", b)
        self.assertIn("tillage, transport", b)
        self.assertIn("Elérhető", b)
        self.assertTrue("6.5" in b or "6,5" in b, b[:3000])
        self.assertIn("Parcella-Hely-5", b)
        self.assertIn('id="equipment-%s"' % o.pk, b)
        self.assertIn(reverse("equipment_edit", kwargs={"pk": o.pk}), b)
        self.assertIn(reverse("equipment_delete", kwargs={"pk": o.pk}), b)
        self.assertIn(reverse("equipment_new"), b)

    def test_unavailable_renders_accessible_text_and_hours_exact(self):
        _, farm, c = make_farm()
        add_eq(farm, name="Gep-Nem-Elérheto-5", caps=["sowing"],
               available=False, hours="4.0")
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn("Nem elérhető", b)
        self.assertTrue("4.0" in b or "4,0" in b, b[:3000])

    def test_all_properties_shown_empty_dash(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        p1 = add_prop(farm, loc, name="Parcella-5-A")
        p2 = add_prop(farm, loc, name="Parcella-5-B")
        o = add_eq(farm, name="Gep-M2M-5", props=[p1, p2])
        b = c.get(reverse("equipment_list")).content.decode()
        m = re.search(r'<tr id="equipment-%s">(.*?)</tr>' % o.pk, b, re.S)
        self.assertIsNotNone(m, "missing equipment row")
        row = m.group(1)
        self.assertIn("Parcella-5-A", row)
        self.assertIn("Parcella-5-B", row)
        o2 = add_eq(farm, name="Gep-Ures-Terulet-5", caps=["tillage"])
        b = c.get(reverse("equipment_list")).content.decode()
        m2 = re.search(r'<tr id="equipment-%s">(.*?)</tr>' % o2.pk, b, re.S)
        self.assertIsNotNone(m2, "missing empty-M2M row")
        self.assertIn("—", m2.group(1))
        self.assertNotIn("hiányzik", b.lower())

    def test_empty_state_colspan(self):
        _, _, c = make_farm(email="eq5empty@pelda.hu")
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn("Nincs elem.", b)
        self.assertIn('colspan="6"', b)

    def test_readonly_viewer_no_actions_no_new(self):
        _, farm, _ = make_farm()
        loc = add_loc(farm)
        prop = add_prop(farm, loc)
        add_eq(farm, props=[prop])
        _, vc = make_member(farm, "eq5viewer@pelda.hu", "viewer")
        b = vc.get(reverse("equipment_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        self.assertIn("Traktor-5-Azonosithato", b)
        self.assertNotIn(reverse("equipment_new"), b)
        self.assertNotIn("equipment_edit", b)
        self.assertNotIn("equipment_delete", b)
        self.assertNotIn("Műveletek", b)
        self.assertNotIn("Actions", b)

    def test_tenant_isolation_no_leak(self):
        _, farm, c = make_farm()
        other_u = User.objects.create_user(
            email="eq5other@pelda.hu", password=PW)
        other_farm = Farm.objects.create(owner=other_u, name="Masik Farm")
        other_loc = add_loc(other_farm, name="MASIK-HELYSZIN-5")
        other_prop = add_prop(other_farm, other_loc, name="MASIK-PARCELLA-5")
        add_eq(other_farm, name="MASIK-FARM-TITKOS-GEP-5",
               props=[other_prop])
        loc = add_loc(farm)
        prop = add_prop(farm, loc, name="SAJAT-PARCELLA-5")
        add_eq(farm, name="SAJAT-GEP-5", props=[prop])
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn("SAJAT-GEP-5", b)
        self.assertNotIn("MASIK-FARM-TITKOS-GEP-5", b)
        self.assertNotIn("MASIK-PARCELLA-5", b)

    def test_english_renders(self):
        _, farm, c = make_farm()
        loc = add_loc(farm)
        prop = add_prop(farm, loc)
        o = add_eq(farm, props=[prop])
        c.post(reverse("set_language"), {"language": "en"})
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        for header in ("Name", "Capabilities", "Availability",
                       "Hours per day", "Plots", "Actions"):
            self.assertIn(header, b)
        self.assertIn("Available", b)
        self.assertIn('id="equipment-%s"' % o.pk, b)

    def test_other_branches_preserved_exact(self):
        _, _, c = make_farm()
        b = c.get(reverse("location_list")).content.decode()
        self.assertIn('<table class="domain-table"', b)
        b = c.get(reverse("property_list")).content.decode()
        self.assertIn('<table class="domain-table domain-table--properties"', b)
        b = c.get(reverse("cycle_list")).content.decode()
        self.assertIn('<ul class="entity-list">', b)
        self.assertNotIn("domain-table", b)


class EquipmentTableMobileScrollTests(TestCase):
    """Task5 mobile390: hours must not split, actions stay reachable."""

    def test_scroll_container_keyboard_accessible(self):
        _, farm, c = make_farm()
        add_eq(farm)
        b = c.get(reverse("equipment_list")).content.decode()
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
        _, farm, c = make_farm(email="eq5css@pelda.hu")
        add_eq(farm)
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn('class="coord"', b)

    def test_scroll_hint_visible(self):
        _, farm, c = make_farm(email="eq5hint@pelda.hu")
        add_eq(farm)
        b = c.get(reverse("equipment_list")).content.decode()
        self.assertIn("scroll-hint", b)
