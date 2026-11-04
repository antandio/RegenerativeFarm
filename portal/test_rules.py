"""Task6 RED: user-owned rule definitions (create/open/edit/save long text).

Contract (docs/SEVEN_TASKS_PLAN.md section 6 + tasks.md item 6):
- RuleCategory owner->User, per-owner unique name; RuleDefinition
  owner->User (NOT farm-scoped), name max 200, body TextField <= 100k,
  is_public bool default False, category mandatory via choose-or-create.
- Visibility: own private + own public + others' public readable;
  others' private 404 everywhere; edit only by author (manager cannot
  edit another's rule even with farm membership).
- Only stored text: no eval/exec/template-render/LLM; escaped display.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from portal.tests import make_user

U = get_user_model()
LONG_HU = "Árvíztűrő tükörfúrógép — hosszú szabályszöveg. "
LONG_EN = "Rule body with äöü ß and 日本語テスト text. "


def _get_models():
    from portal.models import RuleCategory, RuleDefinition
    return RuleCategory, RuleDefinition


class RuleModelTests(TestCase):
    def test_models_importable(self):
        RuleCategory, RuleDefinition = _get_models()
        self.assertTrue(hasattr(RuleCategory, "objects"))
        self.assertTrue(hasattr(RuleDefinition, "objects"))

    def test_category_unique_per_owner_same_name_other_owner_ok(self):
        RuleCategory, _ = _get_models()
        a = make_user("rcat-a@pelda.hu")
        b = make_user("rcat-b@pelda.hu")
        RuleCategory.objects.create(owner=a, name="Pályázat")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RuleCategory.objects.create(owner=a, name="Pályázat")
        # Same name for another owner does not collide.
        RuleCategory.objects.create(owner=b, name="Pályázat")
        self.assertEqual(RuleCategory.objects.filter(name="Pályázat").count(), 2)

    def test_definition_defaults_public_false(self):
        RuleCategory, RuleDefinition = _get_models()
        u = make_user("rdef-a@pelda.hu")
        cat = RuleCategory.objects.create(owner=u, name="Céges")
        obj = RuleDefinition.objects.create(owner=u, category=cat, name="N1", body="b")
        self.assertFalse(obj.is_public)

    def test_name_max200_body_max100k_enforced(self):
        from django.core.exceptions import ValidationError
        RuleCategory, RuleDefinition = _get_models()
        u = make_user("rdef-b@pelda.hu")
        cat = RuleCategory.objects.create(owner=u, name="K")
        with self.assertRaises(ValidationError):
            RuleDefinition(owner=u, category=cat, name="x" * 201, body="b").full_clean()
        with self.assertRaises(ValidationError):
            RuleDefinition(owner=u, category=cat, name="ok", body="x" * 100001).full_clean()


class RuleViewTests(TestCase):
    def setUp(self):
        self.owner = make_user("rule-owner@pelda.hu")
        self.other = make_user("rule-other@pelda.hu")
        self.client.force_login(self.owner)

    def _mk(self, user, name="Sz1", body="body1", public=False, cat=None):
        RuleCategory, RuleDefinition = _get_models()
        if cat is None:
            cat, _ = RuleCategory.objects.get_or_create(owner=user, name=f"Kat-{user.id}")
        return RuleDefinition.objects.create(
            owner=user, category=cat, name=name, body=body, is_public=public)

    def test_sidebar_link_visible(self):
        r = self.client.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Szabályleírások", b)

    def test_create_get_no_write_and_has_csrf(self):
        RuleCategory, RuleDefinition = _get_models()
        n0 = RuleDefinition.objects.count()
        r = self.client.get("/szabalyok/uj/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RuleDefinition.objects.count(), n0)
        self.assertIn("csrfmiddlewaretoken", r.content.decode())

    def test_create_post_choose_existing_category(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="Pályázat")
        r = self.client.post("/szabalyok/uj/", {
            "name": "AKG szabály", "category": str(cat.id),
            "body": "Határidő: március.", "is_public": "on"})
        self.assertEqual(r.status_code, 302)
        obj = RuleDefinition.objects.get(name="AKG szabály")
        self.assertEqual(obj.owner, self.owner)
        self.assertEqual(obj.category, cat)
        self.assertTrue(obj.is_public)

    def test_create_unchecked_public_defaults_false(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="K1")
        r = self.client.post("/szabalyok/uj/", {
            "name": "Privát", "category": str(cat.id), "body": "titkos"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(RuleDefinition.objects.get(name="Privát").is_public)

    def test_create_inline_new_category(self):
        RuleCategory, RuleDefinition = _get_models()
        r = self.client.post("/szabalyok/uj/", {
            "name": "Új kat szabály", "category": "",
            "new_category": "Saját kategória", "body": "szöveg"})
        self.assertEqual(r.status_code, 302)
        obj = RuleDefinition.objects.get(name="Új kat szabály")
        self.assertEqual(obj.category.name, "Saját kategória")
        self.assertEqual(obj.category.owner, self.owner)

    def test_create_rejects_other_users_category(self):
        RuleCategory, RuleDefinition = _get_models()
        foreign = RuleCategory.objects.create(owner=self.other, name="Idegen")
        n0 = RuleDefinition.objects.count()
        r = self.client.post("/szabalyok/uj/", {
            "name": "Forge", "category": str(foreign.id),
            "new_category": "", "body": "x"})
        self.assertIn(r.status_code, (400, 200))
        self.assertEqual(RuleDefinition.objects.count(), n0)

    def test_create_forged_owner_ignored(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="K2")
        r = self.client.post("/szabalyok/uj/", {
            "name": "Forge owner", "category": str(cat.id),
            "body": "x", "owner": str(self.other.id)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(RuleDefinition.objects.get(name="Forge owner").owner, self.owner)

    def test_create_requires_name_and_category(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="K3")
        n0 = RuleDefinition.objects.count()
        r1 = self.client.post("/szabalyok/uj/", {
            "name": "", "category": str(cat.id), "body": "x"})
        self.assertIn(r1.status_code, (400, 200))
        r2 = self.client.post("/szabalyok/uj/", {
            "name": "Név kat nélkül", "category": "",
            "new_category": "", "body": "x"})
        self.assertIn(r2.status_code, (400, 200))
        self.assertEqual(RuleDefinition.objects.count(), n0)

    def test_long_multilingual_body_persists_verbatim(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="K4")
        body = (LONG_HU + LONG_EN) * 600
        self.assertGreater(len(body), 50000)
        self.client.post("/szabalyok/uj/", {
            "name": "Hosszú", "category": str(cat.id), "body": body})
        obj = RuleDefinition.objects.get(name="Hosszú")
        self.assertEqual(obj.body, body)

    def test_body_over_100k_rejected(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="K5")
        n0 = RuleDefinition.objects.count()
        r = self.client.post("/szabalyok/uj/", {
            "name": "Túl hosszú", "category": str(cat.id),
            "body": "x" * 100001})
        self.assertIn(r.status_code, (400, 200))
        self.assertEqual(RuleDefinition.objects.count(), n0)

    def test_private_hidden_from_other_public_readable(self):
        self._mk(self.owner, name="Titkos", public=False)
        pub = self._mk(self.owner, name="Nyílt", public=True)
        self.client.force_login(self.other)
        r = self.client.get("/szabalyok/")
        b = r.content.decode()
        self.assertNotIn("Titkos", b)
        self.assertIn("Nyílt", b)
        self.assertEqual(self.client.get(f"/szabalyok/{pub.pk}/").status_code, 200)

    def test_other_private_detail_edit_404(self):
        RuleCategory, RuleDefinition = _get_models()
        mine = self._mk(self.owner, name="Titkos2", public=False)
        n0 = RuleDefinition.objects.count()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/szabalyok/{mine.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/szabalyok/{mine.pk}/szerkesztes/").status_code, 404)
        r = self.client.post(f"/szabalyok/{mine.pk}/szerkesztes/", {
            "name": "Hacked", "category": str(mine.category_id), "body": "h"})
        self.assertEqual(r.status_code, 404)
        mine.refresh_from_db()
        self.assertEqual(mine.name, "Titkos2")
        self.assertEqual(RuleDefinition.objects.count(), n0)

    def test_other_public_edit_forbidden(self):
        mine = self._mk(self.owner, name="Nyílt2", public=True)
        self.client.force_login(self.other)
        self.assertIn(self.client.get(f"/szabalyok/{mine.pk}/szerkesztes/").status_code, (403, 404))
        r = self.client.post(f"/szabalyok/{mine.pk}/szerkesztes/", {
            "name": "Hacked2", "category": str(mine.category_id), "body": "h"})
        self.assertIn(r.status_code, (403, 404))
        mine.refresh_from_db()
        self.assertEqual(mine.name, "Nyílt2")

    def test_manager_cannot_edit_owners_rule(self):
        from portal.models import Farm, Membership
        farm = Farm.objects.create(owner=self.owner, name="F1")
        Membership.objects.create(farm=farm, user=self.other, role="manager", is_active=True)
        mine = self._mk(self.owner, name="Saját szabály", public=False)
        self.client.force_login(self.other)
        r = self.client.post(f"/szabalyok/{mine.pk}/szerkesztes/", {
            "name": "Manager hack", "category": str(mine.category_id), "body": "h"})
        self.assertIn(r.status_code, (403, 404))
        mine.refresh_from_db()
        self.assertEqual(mine.name, "Saját szabály")

    def test_edit_get_no_write_post_saves(self):
        mine = self._mk(self.owner, name="Szerk", body="régi")
        n0 = _get_models()[1].objects.count()
        r = self.client.get(f"/szabalyok/{mine.pk}/szerkesztes/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_get_models()[1].objects.count(), n0)
        self.assertIn("csrfmiddlewaretoken", r.content.decode())
        r2 = self.client.post(f"/szabalyok/{mine.pk}/szerkesztes/", {
            "name": "Szerk", "category": str(mine.category_id),
            "new_category": "", "body": "új szöveg"})
        self.assertEqual(r2.status_code, 302)
        mine.refresh_from_db()
        self.assertEqual(mine.body, "új szöveg")

    def test_rich_text_escaped_not_executed(self):
        evil = "<script>alert(1)</script> {{ 7*7 }} ignore previous instructions"
        obj = self._mk(self.owner, name="Evil", body=evil, public=True)
        self.assertEqual(_get_models()[1].objects.get(pk=obj.pk).body, evil)
        self.client.force_login(self.other)
        b = self.client.get(f"/szabalyok/{obj.pk}/").content.decode()
        self.assertIn("&lt;script&gt;", b)
        self.assertNotIn("<script>alert(1)</script>", b)

    def test_public_badge_list_and_detail(self):
        self._mk(self.owner, name="Jelvényes", public=True)
        b = self.client.get("/szabalyok/").content.decode()
        self.assertIn("Nyilvános", b)
        obj = _get_models()[1].objects.get(name="Jelvényes")
        b2 = self.client.get(f"/szabalyok/{obj.pk}/").content.decode()
        self.assertIn("Nyilvános", b2)

    def test_no_farm_needed(self):
        fresh = make_user("nofarm@pelda.hu")
        self.client.force_login(fresh)
        r = self.client.get("/szabalyok/")
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get("/szabalyok/uj/")
        self.assertEqual(r2.status_code, 200)

    def test_category_create_reachable(self):
        RuleCategory, _ = _get_models()
        r = self.client.get("/szabalyok/kategoriak/uj/")
        self.assertEqual(r.status_code, 200)
        r2 = self.client.post("/szabalyok/kategoriak/uj/", {"name": "Dedikált"})
        self.assertEqual(r2.status_code, 302)
        self.assertTrue(RuleCategory.objects.filter(owner=self.owner, name="Dedikált").exists())


class RuleEnglishTests(TestCase):
    def setUp(self):
        self.owner = make_user("rule-en@pelda.hu")
        self.client.force_login(self.owner)
        self.client.post("/i18n/setlang/", {"language": "en", "next": "/szabalyok/"})

    def test_en_form_and_detail_no_hungarian(self):
        RuleCategory, RuleDefinition = _get_models()
        cat = RuleCategory.objects.create(owner=self.owner, name="Grant")
        obj = RuleDefinition.objects.create(
            owner=self.owner, category=cat, name="My rule",
            body="Deadline text.", is_public=True)
        for url, en in (("/szabalyok/uj/", "Rule name"),
                        (f"/szabalyok/{obj.pk}/", "My rule")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            b = r.content.decode()
            self.assertIn('<html lang="en">', b, url)
            self.assertIn(en, b, url)
            self.assertNotIn("Szabály neve", b, url)
            self.assertNotIn("Szabály szövege", b, url)
