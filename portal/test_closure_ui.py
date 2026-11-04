"""Closure UI followup RED: rendered livestock list has no stray 0 before
species, and EN header role shows translated owner (not Hungarian).

Covers actual rendered HTML, not catalog contents. Stored journal
descriptions are untouched by design (no translation of user content).
"""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import Farm, FarmLocation, FarmProperty, LivestockGroup

User = get_user_model()


class ClosureUiRegressionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="closure-ui@example.com", password="TesztJelszo123!")
        self.farm = Farm.objects.create(owner=self.owner, name="FarmA")
        loc = FarmLocation.objects.create(
            farm=self.farm, name="Loc", latitude="46.253000",
            longitude="20.141400", timezone="Europe/Budapest")
        self.prop = FarmProperty.objects.create(
            farm=self.farm, location=loc, name="PropA",
            kind="pasture", area_hectares="5.00")
        self.group = LivestockGroup.objects.create(
            farm=self.farm, name="Sheep10", species="sheep",
            headcount=12, housing_property=self.prop,
            breed_label="Merino mix")
        self.client.force_login(self.owner)
        self.client.post(
            "/i18n/setlang/", {"language": "en", "next": "/livestock/"})

    def test_en_header_role_is_owner_not_tulajdonos(self):
        r = self.client.get("/livestock/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("(owner)", b)
        self.assertNotIn("tulajdonos", b)

    def test_livestock_list_no_stray_zero_before_species(self):
        r = self.client.get("/livestock/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        m = re.search(r'livestock-%d.*?</li>' % self.group.pk, b, re.S)
        self.assertIsNotNone(m, "livestock row missing")
        row = m.group(0)
        self.assertNotRegex(row, r'0\s*—')
        self.assertNotRegex(row, r'0(Juh|Sheep)')
        self.assertIn("— Sheep", row)
