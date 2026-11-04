"""Targeted RED-first assertions: English event/map pages fully translated."""
import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from portal.models import Farm, Membership, FarmLocation, FarmProperty

U = get_user_model()
PW = "TesztJelszo123!"

def switch(c, lang, nxt="/attekintes/"):
    return c.post("/i18n/setlang/", {"language": lang, "next": nxt})

def owner(c=None, email="emap@example.com"):
    u = U.objects.create_user(email=email, password=PW)
    f = Farm.objects.create(owner=u, name="F")
    Membership.objects.create(farm=f, user=u, role="owner", is_active=True)
    return u, f

def locprop(f, name="P1", geo=None):
    loc = FarmLocation.objects.create(farm=f, name="L-"+name, latitude=Decimal("46.25"), longitude=Decimal("20.14"), timezone="Europe/Budapest")
    ring = [[20.0,46.0],[20.01,46.0],[20.01,46.01],[20.0,46.01],[20.0,46.0]]
    g = geo if geo is not None else {"type":"Polygon","coordinates":[ring]}
    p = FarmProperty.objects.create(farm=f, location=loc, name=name, kind="field", area_hectares=Decimal("5.00"), boundary_geojson=g)
    return loc, p

class EnEventMapTests(TestCase):
    def setUp(self):
        from datetime import date
        from portal.models import FarmingEvent
        self.u, self.f = owner(email="enmap@example.com")
        self.client.force_login(self.u)
        switch(self.client, "en")
        _, prop = locprop(self.f)
        FarmingEvent.objects.create(
            farm=self.f, property=prop, event_date=date(2024, 5, 1),
            event_type="spraying", description="Test spray",
            created_by=self.u, updated_by=self.u)
    def test_en_event_list_no_hungarian(self):
        b = self.client.get("/events/").content.decode()
        self.assertIn('<html lang="en">', b)
        for en in ("Farming journal", "Operation type", "Spraying", "Fertilisation", "Weeding / crop protection", "From date", "Recorded by"):
            self.assertIn(en, b, en)
        for hu in ("Gazdálkodási napló", "Művelettípus", "Permetezés", "Tápanyag-utánpótlás", "Gyomirtás", "Dátumtól", "Rögzítette", "Határvonal"):
            self.assertNotIn(hu, b, hu)
    def test_en_property_form_and_overview_no_hungarian(self):
        b = self.client.get("/properties/new/").content.decode()
        for en in ("Boundary (GeoJSON)", "Point latitude", "Point longitude", "Plot map"):
            self.assertIn(en, b, en)
        from django.urls import reverse
        from portal.models import FarmProperty as FP
        prop = FP.objects.filter(farm=self.f).first()
        b = self.client.get(reverse("property_edit", kwargs={"pk": prop.pk})).content.decode()
        for en in ("Estimated map area", "not cadastral"):
            self.assertIn(en, b, en)
        for hu in ("Határvonal", "Pont szélesség", "Terület térkép", "Becsült"):
            self.assertNotIn(hu, b, hu)
        b = self.client.get("/properties/").content.decode()
        for en in ("Plot overview map", "Estimated map area", "not cadastral"):
            self.assertIn(en, b, en)
        for hu in ("áttekintő térképe", "Becsült", "kataszteri"):
            self.assertNotIn(hu, b, hu)
    def test_en_geometry_error_english(self):
        loc = FarmLocation.objects.filter(farm=self.f).first()
        r = self.client.post("/properties/new/", {"location": str(loc.pk), "name": "Bad", "kind": "field", "area_hectares": "1.00", "notes": "", "boundary_geojson": '{"type":"MultiPolygon","coordinates":[]}', "point_latitude": "", "point_longitude": ""})
        b = r.content.decode()
        self.assertIn("Only a single Polygon geometry can be saved.", b)

class HuEventMapTests(TestCase):
    def setUp(self):
        self.u, self.f = owner(email="humap@example.com")
        self.client.force_login(self.u)
        switch(self.client, "hu")
    def test_hu_labels(self):
        b = self.client.get("/properties/new/").content.decode()
        self.assertIn("Határvonal (GeoJSON)", b)
        self.assertIn("Terület térkép", b)
