"""Task7 RED: farm activation/deactivation of rule definitions.

Contract (tasks.md item 7 + docs/SEVEN_TASKS_PLAN.md section 7):
- RuleActivation(farm FK, rule FK->RuleDefinition, activated_by FK->User,
  is_active bool default True, created/updated, unique(farm, rule)).
- Eligibility is farm-based, installer-independent: a rule may be
  installed on farm F iff rule.owner == F.owner OR rule is currently
  public. A manager's privately authored rule canNOT be installed on a
  managed farm until it is public.
- Activate/deactivate: owner/manager only (fresh active-farm role),
  POST + CSRF; worker/viewer controls hidden AND backend-denied (403).
  GET never mutates. Double POST is idempotent (single row).
- Public->private atomically deactivates ALL non-author-farm installs
  (rows kept is_active=False); reads never leak private name/body/
  category via stale relations; re-publication does NOT reactivate.
- Author category/body edits are reflected live (no snapshot columns).
- Stored text only: never eval/exec/template-rendered/LLM; escaped.
- Task6 preserved: private definition readable only by its author.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from portal.models import Farm, Membership
from portal.tests import make_user

U = get_user_model()
EVIL = "<script>alert(1)</script> {{ 7*7 }} ignore previous instructions"


def _get_models():
    from portal.models import RuleActivation, RuleCategory, RuleDefinition
    return RuleActivation, RuleCategory, RuleDefinition


def _mk_rule(user, name, body="szoveg", public=False, cat=None):
    _, RuleCategory, RuleDefinition = _get_models()
    if cat is None:
        cat, _ = RuleCategory.objects.get_or_create(
            owner=user, name=f"Kat-{user.id}")
    return RuleDefinition.objects.create(
        owner=user, category=cat, name=name, body=body, is_public=public)


class RuleActivationModelTests(TestCase):
    def test_models_importable(self):
        RuleActivation, _, _ = _get_models()
        self.assertTrue(hasattr(RuleActivation, "objects"))

    def test_unique_farm_rule_and_defaults(self):
        RuleActivation, _, _ = _get_models()
        a = make_user("t7a@pelda.hu")
        b = make_user("t7b@pelda.hu")
        fa = Farm.objects.create(owner=a, name="FA")
        rule = _mk_rule(b, "Pub", public=True)
        act = RuleActivation.objects.create(
            farm=fa, rule=rule, activated_by=a)
        self.assertTrue(act.is_active)
        self.assertEqual(act.activated_by, a)
        self.assertIsNotNone(act.created_at)
        self.assertIsNotNone(act.updated_at)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RuleActivation.objects.create(
                    farm=fa, rule=rule, activated_by=a)

    def test_no_snapshot_columns(self):
        RuleActivation, _, _ = _get_models()
        fields = {f.name for f in RuleActivation._meta.get_fields()}
        for leaked in ("body", "body_snapshot", "snapshot", "name_snapshot",
                       "category_snapshot"):
            self.assertNotIn(leaked, fields)


class FarmRuleViewTests(TestCase):
    def setUp(self):
        self.owner_a = make_user("fa-owner@pelda.hu")
        self.owner_b = make_user("fb-owner@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        self.farm_b = Farm.objects.create(owner=self.owner_b, name="FarmB")
        self.manager = make_user("fa-manager@pelda.hu")
        self.worker = make_user("fa-worker@pelda.hu")
        self.viewer = make_user("fa-viewer@pelda.hu")
        Membership.objects.create(
            farm=self.farm_a, user=self.manager, role="manager", is_active=True)
        Membership.objects.create(
            farm=self.farm_a, user=self.worker, role="worker", is_active=True)
        Membership.objects.create(
            farm=self.farm_a, user=self.viewer, role="viewer", is_active=True)
        self.client.force_login(self.owner_a)

    def _activate_session(self, farm):
        r = self.client.post("/aktiv-farm/", {"farm_id": str(farm.id)})
        self.assertEqual(r.status_code, 302)

    def _mk(self, user, name, **kw):
        return _mk_rule(user, name, **kw)

    def test_no_farm_user_gets_404(self):
        fresh = make_user("t7-nofarm@pelda.hu")
        self.client.force_login(fresh)
        self.assertEqual(self.client.get("/farm-szabalyok/").status_code, 404)

    def test_nav_link_visible(self):
        r = self.client.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("farm-szabalyok", r.content.decode())

    def test_marketplace_only_own_farm_author_or_public(self):
        self._mk(self.owner_a, "A privat", public=False)
        self._mk(self.owner_b, "B nyilvanos", public=True)
        self._mk(self.owner_b, "B titkos", public=False)
        self._activate_session(self.farm_a)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("A privat", b)
        self.assertIn("B nyilvanos", b)
        self.assertNotIn("B titkos", b)

    def test_other_private_activate_404_no_row(self):
        RuleActivation, _, _ = _get_models()
        priv = self._mk(self.owner_b, "B titkos2", public=False)
        self._activate_session(self.farm_a)
        n0 = RuleActivation.objects.count()
        r = self.client.post(f"/farm-szabalyok/{priv.pk}/aktivalas/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(RuleActivation.objects.count(), n0)

    def test_public_activate_success_idempotent_actor(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub2", public=True)
        self._activate_session(self.farm_a)
        r1 = self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        self.assertEqual(r1.status_code, 302)
        r2 = self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        self.assertEqual(r2.status_code, 302)
        rows = RuleActivation.objects.filter(farm=self.farm_a, rule=pub)
        self.assertEqual(rows.count(), 1)
        act = rows.get()
        self.assertTrue(act.is_active)
        self.assertEqual(act.activated_by, self.owner_a)
        self.assertIsNotNone(act.created_at)

    def test_get_activate_never_mutates(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub3", public=True)
        self._activate_session(self.farm_a)
        n0 = RuleActivation.objects.count()
        r = self.client.get(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(RuleActivation.objects.count(), n0)

    def test_get_list_never_mutates_and_has_csrf(self):
        RuleActivation, _, _ = _get_models()
        self._activate_session(self.farm_a)
        n0 = RuleActivation.objects.count()
        r = self.client.get("/farm-szabalyok/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RuleActivation.objects.count(), n0)
        self.assertIn("csrfmiddlewaretoken", r.content.decode())

    def test_manager_can_activate_and_deactivate(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub4", public=True)
        self.client.force_login(self.manager)
        self._activate_session(self.farm_a)
        self.assertEqual(
            self.client.post(
                f"/farm-szabalyok/{pub.pk}/aktivalas/").status_code, 302)
        self.assertTrue(
            RuleActivation.objects.get(
                farm=self.farm_a, rule=pub).is_active)
        self.assertEqual(
            self.client.post(
                f"/farm-szabalyok/{pub.pk}/deaktivalas/").status_code, 302)
        self.assertFalse(
            RuleActivation.objects.get(
                farm=self.farm_a, rule=pub).is_active)

    def test_worker_viewer_backend_denies_and_controls_hidden(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub5", public=True)
        for user in (self.worker, self.viewer):
            self.client.force_login(user)
            self._activate_session(self.farm_a)
            n0 = RuleActivation.objects.count()
            r = self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
            self.assertIn(r.status_code, (403, 404))
            self.assertEqual(RuleActivation.objects.count(), n0)
            b = self.client.get("/farm-szabalyok/").content.decode()
            self.assertEqual(self.client.get("/farm-szabalyok/").status_code, 200)
            self.assertNotIn("aktivalas", b)
            self.assertNotIn("deaktivalas", b)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("aktivalas", b)

    def test_manager_private_authored_needs_public_for_managed_farm(self):
        RuleActivation, _, _ = _get_models()
        mine = self._mk(self.manager, "Manager privat", public=False)
        self.client.force_login(self.manager)
        self._activate_session(self.farm_a)
        n0 = RuleActivation.objects.count()
        r = self.client.post(f"/farm-szabalyok/{mine.pk}/aktivalas/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(RuleActivation.objects.count(), n0)
        mine.is_public = True
        mine.save()
        r2 = self.client.post(f"/farm-szabalyok/{mine.pk}/aktivalas/")
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(RuleActivation.objects.count(), n0 + 1)

    def test_revoke_private_deactivates_other_farms_only(self):
        RuleActivation, RuleCategory, _ = _get_models()
        cat_b, _ = RuleCategory.objects.get_or_create(
            owner=self.owner_b, name="KB")
        pub = self._mk(self.owner_b, "B pub6", public=True, cat=cat_b)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        self.assertEqual(
            self.client.post(
                f"/farm-szabalyok/{pub.pk}/aktivalas/").status_code, 302)
        self.client.force_login(self.owner_b)
        self._activate_session(self.farm_b)
        self.assertEqual(
            self.client.post(
                f"/farm-szabalyok/{pub.pk}/aktivalas/").status_code, 302)
        r = self.client.post(f"/szabalyok/{pub.pk}/szerkesztes/", {
            "name": "B pub6", "category": str(cat_b.id),
            "new_category": "", "body": "most mar privat"})
        self.assertEqual(r.status_code, 302)
        pub.refresh_from_db()
        self.assertFalse(pub.is_public)
        row_a = RuleActivation.objects.get(farm=self.farm_a, rule=pub)
        row_b = RuleActivation.objects.get(farm=self.farm_b, rule=pub)
        self.assertFalse(row_a.is_active)
        self.assertTrue(row_b.is_active)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertNotIn("B pub6", b)
        self.assertNotIn("most mar privat", b)
        self.assertEqual(
            self.client.get(f"/szabalyok/{pub.pk}/").status_code, 404)

    def test_republish_does_not_reactivate(self):
        RuleActivation, RuleCategory, _ = _get_models()
        cat_b, _ = RuleCategory.objects.get_or_create(
            owner=self.owner_b, name="KB2")
        pub = self._mk(self.owner_b, "B pub7", public=True, cat=cat_b)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        self.client.force_login(self.owner_b)
        self.client.post(f"/szabalyok/{pub.pk}/szerkesztes/", {
            "name": "B pub7", "category": str(cat_b.id),
            "new_category": "", "body": "privat"})
        self.client.post(f"/szabalyok/{pub.pk}/szerkesztes/", {
            "name": "B pub7", "category": str(cat_b.id),
            "new_category": "", "body": "ujra publikus",
            "is_public": "on"})
        pub.refresh_from_db()
        self.assertTrue(pub.is_public)
        row = RuleActivation.objects.get(farm=self.farm_a, rule=pub)
        self.assertFalse(row.is_active)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("B pub7", b)
        active_section = b.split("Elérhető leírások")[0]
        self.assertNotIn("ujra publikus", active_section)

    def test_author_edits_reflect_latest_no_snapshot(self):
        RuleActivation, RuleCategory, _ = _get_models()
        cat_b, _ = RuleCategory.objects.get_or_create(
            owner=self.owner_b, name="KB3")
        pub = self._mk(
            self.owner_b, "B pub8", body="regi szoveg", public=True, cat=cat_b)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        cat_new, _ = RuleCategory.objects.get_or_create(
            owner=self.owner_b, name="KB3-uj")
        self.client.force_login(self.owner_b)
        self.client.post(f"/szabalyok/{pub.pk}/szerkesztes/", {
            "name": "B pub8", "category": str(cat_new.id),
            "new_category": "", "body": "friss szoveg", "is_public": "on"})
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("friss szoveg", b)
        self.assertNotIn("regi szoveg", b)
        self.assertIn("KB3-uj", b)

    def test_evil_body_escaped_on_farm_list(self):
        self._mk(self.owner_b, "B evil", body=EVIL, public=True)
        self._activate_session(self.farm_a)
        obj = _get_models()[2].objects.get(name="B evil")
        self.client.post(f"/farm-szabalyok/{obj.pk}/aktivalas/")
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("&lt;script&gt;", b)
        self.assertNotIn("<script>alert(1)</script>", b)

    def test_activefarm_switch_isolates_installs(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub9", public=True)
        own = self._mk(self.owner_b, "B sajat", public=False)
        self._activate_session(self.farm_a)
        self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        self.client.force_login(self.owner_b)
        self._activate_session(self.farm_b)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertNotIn("B pub9", b.split("Aktív szabályok")[1].split(
            "Elérhető")[0] if "Aktív szabályok" in b else b)
        self.client.post(f"/farm-szabalyok/{own.pk}/aktivalas/")
        self.assertTrue(
            RuleActivation.objects.get(farm=self.farm_b, rule=own).is_active)
        self.client.force_login(self.owner_a)
        self._activate_session(self.farm_a)
        b2 = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("B pub9", b2)
        self.assertNotIn("B sajat", b2)

    def test_forged_other_farm_session_denied(self):
        self._activate_session(self.farm_a)
        session = self.client.session
        session["active_farm_id"] = self.farm_b.id
        session.save()
        self.assertEqual(self.client.get("/farm-szabalyok/").status_code, 404)
        r = self.client.post("/aktiv-farm/", {"farm_id": str(self.farm_b.id)})
        self.assertEqual(r.status_code, 404)

    def test_removed_membership_denies(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub10", public=True)
        self.client.force_login(self.manager)
        self._activate_session(self.farm_a)
        Membership.objects.filter(
            farm=self.farm_a, user=self.manager).delete()
        self.assertEqual(self.client.get("/farm-szabalyok/").status_code, 404)
        n0 = RuleActivation.objects.count()
        self.assertEqual(
            self.client.post(
                f"/farm-szabalyok/{pub.pk}/aktivalas/").status_code, 404)
        self.assertEqual(RuleActivation.objects.count(), n0)

    def test_deactivate_idempotent_without_row(self):
        RuleActivation, _, _ = _get_models()
        pub = self._mk(self.owner_b, "B pub11", public=True)
        self._activate_session(self.farm_a)
        n0 = RuleActivation.objects.count()
        r = self.client.post(f"/farm-szabalyok/{pub.pk}/deaktivalas/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(RuleActivation.objects.count(), n0)

    def test_badges_author_category_on_lists(self):
        pub = self._mk(self.owner_b, "B pub12", public=True)
        self._activate_session(self.farm_a)
        b = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("Nyilvános", b)
        self.assertIn(f"Kat-{self.owner_b.id}", b)
        self.client.post(f"/farm-szabalyok/{pub.pk}/aktivalas/")
        b2 = self.client.get("/farm-szabalyok/").content.decode()
        self.assertIn("Nyilvános", b2)


class FarmRuleEnglishTests(TestCase):
    def setUp(self):
        self.owner_a = make_user("fa-en-a@pelda.hu")
        self.owner_b = make_user("fa-en-b@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        Farm.objects.create(owner=self.owner_b, name="FarmB")
        self.client.force_login(self.owner_a)
        self.client.post("/i18n/setlang/", {"language": "en",
                                            "next": "/farm-szabalyok/"})

    def test_en_chrome_no_hungarian(self):
        self.client.post("/aktiv-farm/", {"farm_id": str(self.farm_a.id)})
        r = self.client.get("/farm-szabalyok/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn('<html lang="en">', b)
        self.assertIn("Farm rules", b)
        self.assertNotIn("Farm szabályai", b)
        self.assertNotIn("Szabály neve", b)


class BareSaveAtomicRevokeTests(TestCase):
    """F-2: bare model save (no view) public->private must revoke atomically."""

    def setUp(self):
        self.owner_a = make_user("t7bare-a@pelda.hu")
        self.owner_b = make_user("t7bare-b@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        self.farm_b = Farm.objects.create(owner=self.owner_b, name="FarmB")

    def test_bare_save_public_to_private_revokes_and_republish_stays_inactive(self):
        RuleActivation, RuleCategory, RuleDefinition = (
            _get_models()[0], _get_models()[1], _get_models()[2])
        cat, _ = RuleCategory.objects.get_or_create(owner=self.owner_b, name="KBARE")
        rule = RuleDefinition.objects.create(
            owner=self.owner_b, category=cat, name="Bare pub",
            body="szoveg", is_public=True)
        from portal.farm_rules import activate_rule
        activate_rule(farm=self.farm_a, rule=rule, actor=self.owner_a)
        activate_rule(farm=self.farm_b, rule=rule, actor=self.owner_b)
        # Bare save bypassing every view.
        rule.is_public = False
        rule.save()
        rule.refresh_from_db()
        self.assertFalse(rule.is_public)
        self.assertFalse(
            RuleActivation.objects.get(farm=self.farm_a, rule=rule).is_active)
        self.assertTrue(
            RuleActivation.objects.get(farm=self.farm_b, rule=rule).is_active)
        # Re-publication must NOT auto-enable.
        rule.is_public = True
        rule.save()
        self.assertFalse(
            RuleActivation.objects.get(farm=self.farm_a, rule=rule).is_active)

    def test_save_and_revoke_are_atomic_on_revoke_failure(self):
        from unittest.mock import patch
        RuleActivation, RuleCategory, _ = (
            _get_models()[0], _get_models()[1], _get_models()[2])
        cat, _ = RuleCategory.objects.get_or_create(owner=self.owner_b, name="KBATOM")
        rule = _mk_rule(self.owner_b, "Atom pub", public=True, cat=cat)
        from portal.farm_rules import activate_rule
        activate_rule(farm=self.farm_a, rule=rule, actor=self.owner_a)
        rule.is_public = False
        with patch("portal.domain_models.RuleActivation") as mock_act:
            mock_act.objects.filter.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                rule.save()
        rule.refresh_from_db()
        self.assertTrue(rule.is_public)
        self.assertTrue(
            RuleActivation.objects.get(farm=self.farm_a, rule=rule).is_active)


class StaleActivationRaceTests(TestCase):
    """F-1: activation with a stale public instance after privatization."""

    def setUp(self):
        self.owner_a = make_user("t7race-a@pelda.hu")
        self.owner_b = make_user("t7race-b@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        Farm.objects.create(owner=self.owner_b, name="FarmB")

    def test_stale_public_instance_cannot_activate_after_private(self):
        from django.http import Http404
        RuleActivation, RuleCategory, RuleDefinition = (
            _get_models()[0], _get_models()[1], _get_models()[2])
        cat, _ = RuleCategory.objects.get_or_create(owner=self.owner_b, name="KBRACE")
        rule = RuleDefinition.objects.create(
            owner=self.owner_b, category=cat, name="Race pub",
            body="szoveg", is_public=True)
        stale = RuleDefinition.objects.get(pk=rule.pk)
        self.assertTrue(stale.is_public)
        # Author privatizes via a fresh row (the "concurrent" save).
        fresh = RuleDefinition.objects.get(pk=rule.pk)
        fresh.is_public = False
        fresh.save()
        from portal.farm_rules import activate_rule
        with self.assertRaises(Http404):
            activate_rule(farm=self.farm_a, rule=stale, actor=self.owner_a)
        self.assertFalse(
            RuleActivation.objects.filter(farm=self.farm_a, rule=rule).exists())
        # Re-publication must not surface the raced row as active.
        fresh.is_public = True
        fresh.save()
        self.assertFalse(
            RuleActivation.objects.filter(
                farm=self.farm_a, rule=rule, is_active=True).exists())


class BulkUpdateGuardTests(TestCase):
    """QuerySet.update / bulk_update must not silently bypass the revoke."""

    def setUp(self):
        self.owner = make_user("t7bulk@pelda.hu")
        make_user("t7bulk-b@pelda.hu")

    def test_queryset_update_is_public_blocked(self):
        _, _, RuleDefinition = _get_models()[0], _get_models()[1], _get_models()[2]
        rule = _mk_rule(self.owner, "Bulk pub", public=True)
        with self.assertRaises(ValueError):
            RuleDefinition.objects.filter(pk=rule.pk).update(is_public=False)
        rule.refresh_from_db()
        self.assertTrue(rule.is_public)

    def test_queryset_update_without_is_public_allowed(self):
        _, _, RuleDefinition = _get_models()[0], _get_models()[1], _get_models()[2]
        rule = _mk_rule(self.owner, "Bulk name", public=True)
        n = RuleDefinition.objects.filter(pk=rule.pk).update(name="Atnevezve")
        self.assertEqual(n, 1)
        rule.refresh_from_db()
        self.assertEqual(rule.name, "Atnevezve")
        self.assertTrue(rule.is_public)

    def test_bulk_update_is_public_blocked(self):
        _, _, RuleDefinition = _get_models()[0], _get_models()[1], _get_models()[2]
        rule = _mk_rule(self.owner, "Bulk2 pub", public=True)
        rule.is_public = False
        with self.assertRaises(ValueError):
            RuleDefinition.objects.bulk_update([rule], ["is_public"])
        rule.refresh_from_db()
        self.assertTrue(rule.is_public)
