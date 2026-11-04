"""Task 2 RED: sectioned, accessible, responsive farm page UI.

Source-structure assertions only (sections/labels/helptexts/formset markup/CSS
scope); browser overflow is verified independently on the deployed build.
"""
import pathlib
import re

from django.test import TestCase, Client
from django.contrib.auth import get_user_model

from .models import Farm, FarmExtraField, Membership

User = get_user_model()
PW = "TesztJelszo123!"

CORE_FIELDS = [
    "name", "region_text", "climate_text", "farming_direction_text",
    "soil_notes", "water_notes", "crops_livestock_notes",
    "equipment_labour_notes", "goals_notes", "constraints_notes",
]

CORE_POST = {f: "" for f in CORE_FIELDS}
CORE_POST["name"] = "T2 Farm"

SECTIONS = [
    ("farm-identity", ["name"]),
    ("farm-main", ["region_text", "climate_text", "farming_direction_text"]),
    ("farm-desc", ["soil_notes", "water_notes", "crops_livestock_notes",
                   "equipment_labour_notes"]),
    ("farm-goals", ["goals_notes", "constraints_notes"]),
]


def make_owner(email="t2owner@pelda.hu"):
    u = User.objects.create_user(email=email, password=PW)
    farm = Farm.objects.create(owner=u, name="T2 Farm")
    c = Client()
    c.force_login(u)
    return u, farm, c


def make_member(farm, email, role):
    u = User.objects.create_user(email=email, password=PW)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    c = Client()
    c.force_login(u)
    return u, c


class FarmPageUITests(TestCase):
    def test_sections_render_with_headings(self):
        _, _, c = make_owner()
        b = c.get("/gazdasagom/").content.decode()
        for section_id, _ in SECTIONS:
            self.assertIn('class="farm-section"', b)
            self.assertIn(f'aria-labelledby="{section_id}-title"', b)
            self.assertIn(f'id="{section_id}-title"', b)
        self.assertIn('id="farm-custom-title"', b)

    def test_all_core_fields_labels_and_helptexts_kept(self):
        _, _, c = make_owner()
        b = c.get("/gazdasagom/").content.decode()
        for name in CORE_FIELDS:
            self.assertIn(f'name="{name}"', b, f"core field {name} missing")
        for label in ["Gazdaság neve", "Régió", "Éghajlati",
                      "gazdálkodási irány", "Talaj", "Vízellátás",
                      "Növények", "Gépek", "Célok", "Korlátok"]:
            self.assertIn(label, b, f"label {label!r} missing")
        # every core field keeps a compact helptext
        self.assertGreaterEqual(b.count('class="helptext"'), 10)
        # error groups stay grouped on invalid submit
        bad = dict(CORE_POST)
        bad["name"] = ""
        eb = c.post("/gazdasagom/", bad).content.decode()
        self.assertIn('class="errorlist"', eb)
        self.assertIn('class="farm-section"', eb)
        # skip link for keyboard users
        self.assertIn('href="#main-content"', b)

    def test_fields_grouped_in_their_sections(self):
        _, _, c = make_owner()
        b = c.get("/gazdasagom/").content.decode()
        for section_id, fields in SECTIONS:
            m = re.search(
                rf'<section[^>]*aria-labelledby="{section_id}-title".*?</section>',
                b, re.S)
            self.assertIsNotNone(m, f"section {section_id} missing")
            for name in fields:
                self.assertIn(f'name="{name}"', m.group(0),
                              f"{name} not in section {section_id}")

    def test_custom_formset_markup_create_edit_delete(self):
        u, farm, c = make_owner()
        ex = FarmExtraField.objects.create(farm=farm, key="Öntözés", value="kút")
        b = c.get("/gazdasagom/").content.decode()
        self.assertIn("extras-TOTAL_FORMS", b)
        self.assertIn("extras-INITIAL_FORMS", b)
        self.assertIn("extras-MAX_NUM_FORMS", b)
        self.assertIn("Saját mezők", b)
        self.assertIn("Megnevezés", b)
        self.assertIn("Érték", b)
        # edit path: hidden id for the saved row
        self.assertIn(f'value="{ex.pk}"', b)
        self.assertIn('type="hidden"', b)
        # delete control only for the saved row
        self.assertIn("DELETE", b)
        # per-row helptexts kept
        self.assertIn("64", b)
        self.assertIn("500", b)

    @staticmethod
    def _main(b):
        return b.split("<main")[1].split("</main>")[0]

    def test_readonly_roles_have_no_write_controls(self):
        _, farm, _ = make_owner()
        for email, role in [("t2worker@pelda.hu", "worker"),
                            ("t2viewer@pelda.hu", "viewer")]:
            _, c = make_member(farm, email, role)
            r = c.get("/gazdasagom/")
            self.assertEqual(r.status_code, 200)
            m = self._main(r.content.decode())
            self.assertNotIn('type="submit"', m,
                             f"{role} must not see a submit control")
            self.assertNotIn("<form", m,
                             f"{role} must not see an editable form")
            self.assertNotIn("DELETE", m,
                             f"{role} must not see delete controls")
            self.assertNotIn("extras-TOTAL_FORMS", m,
                             f"{role} must not see an editable formset")
            # read-only values still visible, no misleading write UI
            self.assertIn("T2 Farm", m)

    def test_create_flow_kept_for_anonymous_owner(self):
        u = User.objects.create_user(email="t2fresh@pelda.hu", password=PW)
        c = Client()
        c.force_login(u)
        m = self._main(c.get("/gazdasagom/").content.decode())
        self.assertIn("Gazdaság létrehozása", m)
        self.assertIn('type="submit"', m)

    def test_scoped_css_no_global_overflow(self):
        css = (pathlib.Path(__file__).parent / "static/portal/app.css").read_text()
        for cls in [".farm-section", ".farm-grid", ".farm-field",
                    ".farm-custom-row", ".farm-readonly"]:
            self.assertIn(cls, css, f"missing scoped rule {cls}")
        m = re.search(r"\.farm-grid\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing .farm-grid rule")
        for prop in ["min-width: 0", "max-width: 100%", "overflow-wrap: anywhere"]:
            self.assertIn(prop, m.group(1), prop)
        # single column on narrow viewports (390 comfortable)
        self.assertTrue(
            re.search(r"@media[^{]*max-width:\s*900px[^{]*\{[^}]*\.farm-grid[^}]*"
                      r"grid-template-columns:\s*1fr", css, re.S),
            "missing narrow single-column .farm-grid rule")

    def test_hu_and_en_render(self):
        _, _, c = make_owner()
        hu = c.get("/gazdasagom/").content.decode()
        self.assertIn("Gazdaságom", hu)
        c.cookies["django_language"] = "en"
        en = c.get("/gazdasagom/").content.decode()
        self.assertEqual(c.get("/gazdasagom/").status_code, 200)
        self.assertIn('lang="en"', en)
        for _, fields in SECTIONS:
            for name in fields:
                self.assertIn(f'name="{name}"', en)
