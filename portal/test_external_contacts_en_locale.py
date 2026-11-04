"""Task4 EN locale regression: external contact UI strings in English.

English comments only. User stored content stays Hungarian intentionally.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

User = get_user_model()


def make_owner(email="ecloc-owner@example.com"):
    from portal.models import Farm
    u = User.objects.create_user(email=email, password="TesztJelszo123!")
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def seed_contact(farm):
    from portal.models import ExternalContact
    return ExternalContact.objects.create(
        farm=farm, name="Kovács Béla", phone="+36 30 123 4567",
        email="bela@example.com", services="Szántás\nVetés",
        notes="Rövid határidővel dolgozik.",
        availability="Hétvégén elérhető")


class ExternalContactEnLocaleTests(TestCase):
    def setUp(self):
        self.u, self.f = make_owner()
        self.c = Client()
        self.c.force_login(self.u)
        self.obj = seed_contact(self.f)
        self.c.post("/i18n/setlang/", {"language": "en", "next": "/"})

    def test_detail_headings_english_no_hungarian(self):
        r = self.c.get(f"/tagok/kulsok/{self.obj.pk}/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Requestable work", b)
        self.assertIn("Short description", b)
        self.assertNotIn("Kérhető munkák", b)
        self.assertNotIn("Rövid leírás", b)
        # User stored content remains Hungarian intentionally.
        self.assertIn("Szántás", b)
        self.assertIn("Rövid határidővel dolgozik.", b)

    def test_list_details_link_english(self):
        r = self.c.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Details", b)
        # Name link remains.
        self.assertIn("Kovács Béla", b)
