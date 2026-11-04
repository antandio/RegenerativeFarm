"""Task5 RED: external hireable nonmember contacts, owner-only staff CRUD.

Asserts (all must FAIL before production code exists):
- ExternalContact model: farm/name mandatory, phone OR email required,
  services list required (newline canonical), notes short description.
- Contacts never create User/Membership rows.
- Owner-only: viewer/manager/worker denied on read AND write.
- Tenant: forged farm id / other farm contact -> 404, no leak in list.
- CRUD persistence + HU/EN rendered strings.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="ec-owner@example.com"):
    from portal.models import Farm
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def add_member(farm, email, role):
    from portal.models import Membership
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def login(client, user):
    client.force_login(user)


VALID = {
    "name": "Kovács Béla",
    "phone": "+36 30 123 4567",
    "email": "",
    "services": "Szántás\nVetés",
    "notes": "Rövid határidővel gyorsan dolgozik.",
    "availability": "Hétvégén elérhető",
}


class ExternalContactModelTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()

    def test_model_requires_name_and_contact_and_services(self):
        from portal.models import ExternalContact
        from django.core.exceptions import ValidationError
        # blank name rejected
        with self.assertRaises(ValidationError):
            ExternalContact.objects.create(
                farm=self.f, name="   ", phone="+36 30 111 1111",
                services="Szántás")
        # no phone and no email rejected (model + form level)
        with self.assertRaises(ValidationError):
            ExternalContact.objects.create(
                farm=self.f, name="Név", phone="", email="",
                services="Szántás")
        # whitespace-only phone/email rejected as blank
        with self.assertRaises(ValidationError):
            ExternalContact.objects.create(
                farm=self.f, name="Név", phone="   ", email="  ",
                services="Szántás")
        # blank/whitespace services rejected
        with self.assertRaises(ValidationError):
            ExternalContact.objects.create(
                farm=self.f, name="Név", phone="+36 30 111 1111",
                services="  \n  ")

    def test_model_email_validated_and_canonical_services(self):
        from portal.models import ExternalContact
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            ExternalContact.objects.create(
                farm=self.f, name="Név", email="not-an-email",
                services="Szántás")
        c = ExternalContact.objects.create(
            farm=self.f, name="  Kovács Béla  ",
            email="bela@example.com", services=" Szántás \n\n Vetés \n")
        self.assertEqual(c.services, "Szántás\nVetés")
        self.assertEqual(c.name, "Kovács Béla")

    def test_contact_creates_no_user_or_membership(self):
        from portal.models import ExternalContact
        from portal.models import Membership
        n_users = User.objects.count()
        n_mem = Membership.objects.count()
        ExternalContact.objects.create(
            farm=self.f, name="Külsős", email="kulsos@example.com",
            services="Szántás")
        self.assertEqual(User.objects.count(), n_users)
        self.assertEqual(Membership.objects.count(), n_mem)
        self.assertFalse(User.objects.filter(email="kulsos@example.com").exists())


class ExternalContactViewTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.c = Client()

    def test_owner_crud_persists(self):
        from portal.models import ExternalContact
        login(self.c, self.u)
        r = self.c.post("/tagok/kulsok/uj/", VALID)
        self.assertIn(r.status_code, (302, 303))
        obj = ExternalContact.objects.get(farm=self.f, name="Kovács Béla")
        self.assertEqual(obj.services, "Szántás\nVetés")
        # detail reachable, contact details rendered with mailto/tel
        r = self.c.get(f"/tagok/kulsok/{obj.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Kovács Béla")
        self.assertContains(r, "tel:")
        # edit persists
        r = self.c.post(f"/tagok/kulsok/{obj.pk}/szerkesztes/", {
            **VALID, "name": "Kovács Béla Jr.", "email": "jr@example.com",
            "phone": "",
        })
        self.assertIn(r.status_code, (302, 303))
        obj.refresh_from_db()
        self.assertEqual(obj.name, "Kovács Béla Jr.")
        self.assertEqual(obj.email, "jr@example.com")
        r = self.c.get(f"/tagok/kulsok/{obj.pk}/")
        self.assertContains(r, "mailto:jr@example.com")
        # delete removes
        r = self.c.post(f"/tagok/kulsok/{obj.pk}/torles/")
        self.assertIn(r.status_code, (302, 303))
        self.assertFalse(ExternalContact.objects.filter(pk=obj.pk).exists())

    def test_create_validation_errors_blank_and_no_contact(self):
        login(self.c, self.u)
        bad = dict(VALID, name="   ")
        r = self.c.post("/tagok/kulsok/uj/", bad)
        self.assertEqual(r.status_code, 400)
        bad = dict(VALID, phone="", email="  ")
        r = self.c.post("/tagok/kulsok/uj/", bad)
        self.assertEqual(r.status_code, 400)

    def test_non_owners_denied_read_and_write(self):
        from portal.models import ExternalContact
        obj = ExternalContact.objects.create(
            farm=self.f, name="Titkos", phone="+36 30 999 9999",
            services="Szántás")
        for role in ("viewer", "manager", "worker"):
            user = add_member(self.f, f"ec-{role}@example.com", role)
            c = Client()
            login(c, user)
            self.assertIn(c.get("/tagok/kulsok/").status_code, (403, 404),
                          msg=role)
            self.assertIn(c.get(f"/tagok/kulsok/{obj.pk}/").status_code,
                          (403, 404), msg=role)
            self.assertIn(c.post("/tagok/kulsok/uj/", VALID).status_code,
                          (403, 404), msg=role)
            self.assertIn(
                c.post(f"/tagok/kulsok/{obj.pk}/szerkesztes/", VALID).status_code,
                (403, 404), msg=role)
            self.assertIn(
                c.post(f"/tagok/kulsok/{obj.pk}/torles/").status_code,
                (403, 404), msg=role)

    def test_tenant_isolation_no_leak(self):
        from portal.models import ExternalContact
        obj = ExternalContact.objects.create(
            farm=self.f, name="Titkos kontakt", phone="+36 30 999 9999",
            email="titkos@example.com", services="Szántás")
        u2, _f2 = owner_farm("ec-second@example.com")
        c2 = Client()
        login(c2, u2)
        self.assertEqual(c2.get(f"/tagok/kulsok/{obj.pk}/").status_code, 404)
        r = c2.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Titkos kontakt")
        self.assertNotContains(r, "titkos@example.com")
        self.assertNotContains(r, "+36 30 999 9999")

    def test_staff_page_owner_only_regression(self):
        # members page itself stays owner-only (existing security unchanged)
        viewer = add_member(self.f, "ec-staff-viewer@example.com", "viewer")
        c = Client()
        login(c, viewer)
        self.assertIn(c.get("/tagok/").status_code, (403, 404))

    def test_hu_strings_rendered(self):
        login(self.c, self.u)
        r = self.c.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Külső", b)
        self.assertIn("Kérhető munkák", b)

    def test_en_strings_rendered_no_hu_leak(self):
        login(self.c, self.u)
        self.c.post("/i18n/setlang/", {"language": "en", "next": "/tagok/kulsok/"})
        r = self.c.get("/tagok/kulsok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn('<html lang="en">', b)
        for en in ("External contacts", "By requestable work", "New external contact"):
            self.assertIn(en, b, f"missing EN label: {en}")
        self.assertIn(
            "External people available for contract work. "
            "Not members: they get no login, invitation, or role.",
            b,
            "missing EN description",
        )
        for hu in ("Külső", "Kérhető munkák", "Új külső kapcsolattartó",
                   "Bérmunkára felbérelhető"):
            self.assertNotIn(hu, b, f"HU leaked: {hu}")

    def test_en_create_edit_form_name_label_translated(self):
        from portal.models import ExternalContact
        login(self.c, self.u)
        self.c.post("/i18n/setlang/", {"language": "en", "next": "/tagok/kulsok/"})
        r = self.c.get("/tagok/kulsok/uj/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn('<html lang="en">', b)
        self.assertIn("Name", b, "missing EN form label: Name")
        self.assertNotIn(">Név:<", b, "HU form label leaked: Név")
        obj = ExternalContact.objects.create(
            farm=self.f, name="Külsős", phone="+36 30 111 1111",
            services="Szántás")
        r = self.c.get(f"/tagok/kulsok/{obj.pk}/szerkesztes/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Name", b, "missing EN edit form label: Name")
        self.assertNotIn(">Név:<", b, "HU edit form label leaked: Név")
