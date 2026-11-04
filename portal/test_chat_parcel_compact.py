"""Task 3 review fixes: compact parcel picker + localized short hints.

RED tests for the browser screenshot review findings:
- EN page must not leak the Hungarian cross-parcel explanatory sentence.
- Picker CSS must be compact (44-56px rows) with an internal scroll region.
"""
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.domain_models import FarmLocation, FarmProperty
from portal.models import ConsentEvent, Farm

User = get_user_model()

HU_CROSS_PARCEL = (
    "Egy vagy több parcella is kiválasztható; a kereszt-parcellás kérdések "
    "több parcella együttes hatását vizsgálják."
)


def make_user(email):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


class CompactPickerFixture(TestCase):
    def setUp(self):
        self.u = make_user("compact@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="CompactFarm")
        ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="granted")
        loc = FarmLocation.objects.create(
            farm=self.farm, name="Loc", latitude="46.250000",
            longitude="20.140000", timezone="Europe/Budapest")
        self.prop_a = FarmProperty.objects.create(
            farm=self.farm, location=loc, name="ParcelAlpha",
            kind="field", area_hectares="5.00")
        self.prop_b = FarmProperty.objects.create(
            farm=self.farm, location=loc, name="ParcelBeta",
            kind="field", area_hectares="3.00")
        self.prop_c = FarmProperty.objects.create(
            farm=self.farm, location=loc, name="ParcelGamma",
            kind="orchard", area_hectares="2.00")

    def _client(self):
        c = Client()
        c.force_login(self.u)
        return c


class CompactLocaleTests(CompactPickerFixture):
    def test_english_page_has_no_hungarian_cross_parcel_sentence(self):
        c = self._client()
        c.cookies["django_language"] = "en"
        r = c.get("/chat/", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        self.assertNotIn(HU_CROSS_PARCEL, html)
        # One short localized cost sentence stays visible without details.
        self.assertIn("Cost only arises from explicit sending.", html)

    def test_hungarian_page_keeps_single_short_hint(self):
        c = self._client()
        r = c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        self.assertNotIn(HU_CROSS_PARCEL, html)
        self.assertIn("Ha nincs kiválasztva semmi", html)
        self.assertIn("Költség csak a kifejezett küldéssel keletkezik.", html)


class CompactCssTests(CompactPickerFixture):
    CSS_PATH = Path("portal/static/portal/chat_parcels.css")

    def _css(self):
        return self.CSS_PATH.read_text(encoding="utf-8")

    def test_picker_css_is_compact_with_internal_scroll(self):
        css = self._css()
        # Internal scroll budget for many rows, not whole-page overflow.
        self.assertIn("220px", css)
        self.assertIn("overflow-y", css)
        self.assertIn("auto", css)
        # Container-responsive multi-column list (no viewport breakpoint).
        self.assertRegex(css, r"repeat\s*\(\s*auto-fit")
        self.assertIn("min(100%, 240px)", css.replace("min(100%,240px)", "min(100%, 240px)"))
        self.assertNotIn("@media", css)
        # Native checkbox inline reset beating the global label/input styles.
        self.assertIn('.parcel-item label', css)
        self.assertIn('input[type="checkbox"]', css)
        label_block = re.search(
            r"\.parcel[^{]*label\s*\{([^}]*)\}", css, re.S)
        self.assertIsNotNone(label_block, msg="parcel label reset missing")
        self.assertIn("margin: 0", label_block.group(1).replace("margin:0", "margin: 0"))
        # Compact row budget: min touch target but small padding.
        item_block = re.search(r"\.parcel-item\s*\{([^}]*)\}", css, re.S)
        self.assertIsNotNone(item_block, msg=".parcel-item rule missing")
        body = item_block.group(1)
        self.assertIn("min-height: 44px", body.replace("min-height:44px", "min-height: 44px"))
        m = re.search(r"padding\s*:\s*(\d+)px", body)
        self.assertIsNotNone(m, msg="compact vertical padding missing")
        self.assertLessEqual(int(m.group(1)) * 2 + 44, 64)

    def test_picker_html_has_named_scroll_region_and_quota_details(self):
        c = self._client()
        r = c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        self.assertIn('role="region"', html)
        self.assertIn('tabindex="0"', html)
        self.assertIn("<details", html)
        # Technical quota/context stays available inside the disclosure.
        self.assertIn("Keret", html)
        self.assertIn("Környezet mérete", html)
