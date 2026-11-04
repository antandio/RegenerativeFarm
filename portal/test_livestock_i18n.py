"""Task4 localization RED: EN rendered livestock pages carry translated group/event/work labels.

Fails until HU/EN catalogs gain the livestock strings and compilemessages runs.
Covers actual rendered pages (list/detail/event form/work form/sidebar),
not catalog file contents.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import Farm, FarmLocation, FarmProperty, LivestockGroup

User = get_user_model()


def switch(client, lang, next_url="/livestock/"):
    return client.post("/i18n/setlang/", {"language": lang, "next": next_url})


class LivestockEnglishRenderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="ls-i18n-en@example.com", password="TesztJelszo123!")
        self.farm = Farm.objects.create(owner=self.owner, name="Farm ls-i18n-en")
        loc = FarmLocation.objects.create(
            farm=self.farm, name="Loc", latitude="46.253000",
            longitude="20.141400", timezone="Europe/Budapest")
        self.prop = FarmProperty.objects.create(
            farm=self.farm, location=loc, name="Pasture parcel",
            kind="pasture", area_hectares="5.00")
        self.group = LivestockGroup.objects.create(
            farm=self.farm, name="Herd", species="sheep",
            headcount=10, housing_property=self.prop)
        self.client.force_login(self.owner)
        switch(self.client, "en")

    def test_en_list_translates_group_labels(self):
        r = self.client.get("/livestock/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn('<html lang="en">', b)
        for en in ("Livestock", "Sheep", "Headcount", "Housing", "New livestock group"):
            self.assertIn(en, b, f"missing EN label: {en}")
        for hu in ("Állatállomány", "Juh", "Létszám", "Szállás", "Új állatcsoport"):
            self.assertNotIn(hu, b, f"HU leaked: {hu}")

    def test_en_detail_translates_event_and_work_labels(self):
        r = self.client.get(f"/livestock/{self.group.pk}/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        for en in ("Dated events", "Planned tasks", "New event", "New planned livestock task"):
            self.assertIn(en, b, f"missing EN label: {en}")
        for hu in ("Dátumozott események", "Tervezett feladatok", "Új esemény", "Új tervezett állatmunka"):
            self.assertNotIn(hu, b, f"HU leaked: {hu}")

    def test_en_forms_translate_event_and_work_fields(self):
        r = self.client.get(f"/livestock/{self.group.pk}/events/new/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        for en in ("Event type", "Headcount change"):
            self.assertIn(en, b, f"missing EN label: {en}")
        self.assertNotIn("Eseménytípus", b)
        r = self.client.get(f"/livestock/{self.group.pk}/work/new/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        for en in ("Task type", "Task title"):
            self.assertIn(en, b, f"missing EN label: {en}")
        self.assertNotIn("Munka típusa", b)
