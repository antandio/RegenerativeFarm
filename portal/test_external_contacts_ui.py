"""Task4 UI regression: responsive external contact list/detail cards.

RED-first: asserts modern card semantics on rendered HTML.
List: scoped CSS link, card grid, count, initials, service chips,
tel:/mailto: actions, detail/edit links, members link, empty state.
Detail: identity header, info/services/notes sections, actions.
English comments only; UI strings stay HU/EN via existing catalog.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

User = get_user_model()


def make_owner(email="ecui-owner@example.com"):
    from portal.models import Farm
    u = User.objects.create_user(email=email, password="TesztJelszo123!")
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def seed_contact(farm, name="Kovács Béla"):
    from portal.models import ExternalContact
    return ExternalContact.objects.create(
        farm=farm, name=name, phone="+36 30 123 4567",
        email="bela@example.com", services="Szántás\nVetés",
        notes="Rövid határidővel dolgozik.",
        availability="Hétvégén elérhető")


class ExternalContactListUiTests(TestCase):
    def setUp(self):
        self.u, self.f = make_owner()
        self.c = Client()
        self.c.force_login(self.u)

    def test_list_card_semantics(self):
        seed_contact(self.f)
        r = self.c.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        # Scoped stylesheet with cache-busting query tag.
        self.assertIn("external_contacts.css?v=", b)
        # Responsive card grid with one card.
        self.assertIn("ec-grid", b)
        self.assertIn("ec-card", b)
        # Count badge next to heading.
        self.assertIn("ec-count", b)
        # Initials avatar derived from name.
        self.assertIn("ec-initials", b)
        # Service chips, one per service line.
        self.assertIn("ec-chip", b)
        self.assertIn("Szántás", b)
        self.assertIn("Vetés", b)
        # Contact actions use tel: and mailto: schemes.
        self.assertIn("tel:+36301234567", b)
        self.assertIn("mailto:bela@example.com", b)
        # Detail and edit actions reachable from the card.
        self.assertIn("/tagok/kulsok/", b)
        self.assertIn("szerkesztes", b)
        # Members cross-link preserved.
        self.assertIn('href="/tagok/"', b)

    def test_list_empty_state(self):
        r = self.c.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("ec-empty", b)
        self.assertIn("Még nincs külső kapcsolattartó.", b)

    def test_list_long_values_wrapped(self):
        seed_contact(self.f, name="Nagyon Hosszú Nevű Külső Kapcsolattartó Példa")
        from portal.models import ExternalContact
        obj = ExternalContact.objects.get(farm=self.f)
        obj.services = "RendkívülHosszúSzolgáltatásnévSorTörésNélkülTesztelésre"
        obj.save()
        r = self.c.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("ec-card", b)
        self.assertIn("ec-wrap", b)


class ExternalContactDetailUiTests(TestCase):
    def setUp(self):
        self.u, self.f = make_owner(email="ecuid-owner@example.com")
        self.c = Client()
        self.c.force_login(self.u)
        self.obj = seed_contact(self.f)

    def test_detail_sections_and_actions(self):
        r = self.c.get(f"/tagok/kulsok/{self.obj.pk}/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("external_contacts.css?v=", b)
        # Identity header with initials + name.
        self.assertIn("ec-identity", b)
        self.assertIn("ec-initials", b)
        self.assertIn("Kovács Béla", b)
        # Sections reuse existing HU strings.
        self.assertIn("Elérhetőség", b)
        self.assertIn("Kérhető munkák", b)
        self.assertIn("Rövid leírás", b)
        self.assertIn("Hétvégén elérhető", b)
        # Contact + management actions reachable.
        self.assertIn("tel:+36301234567", b)
        self.assertIn("mailto:bela@example.com", b)
        self.assertIn("ec-actions", b)
        self.assertIn("szerkesztes", b)
        self.assertIn("torles", b)
