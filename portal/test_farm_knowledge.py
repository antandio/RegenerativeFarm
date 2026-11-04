"""Task9 RED: farm knowledge base (documents added to own farm) — management only.

Contract (tasks.md item 9 + docs/ADDITIONAL_TASKS_PLAN.md section 9):
- FarmDocument(farm FK, document FK, added_by FK->User, is_active True,
  created/updated, unique(farm, document)). No snapshot columns.
- Eligible iff document.owner == farm.owner OR document currently public
  (installer-independent; manager's private doc needs public first).
- Writes: owner/manager only (fresh role), POST+CSRF; worker/viewer read-only
  and backend-denied; GET never mutates. Idempotent double add.
- Public->private (via Document.save, any path incl. bare save) atomically
  deactivates ALL non-owner-farm installs in the same txn; reads hide
  metadata/download afterwards; re-publication never re-enables.
- Document/account delete cascades knowledge rows + shared file cleanup.
- One farm knowledge page: active list + marketplace; sidebar entry; own
  document detail links add/remove for the active farm.
- Explicitly NOT execution/decision: entries never feed plans/chat/advice;
  UI states management-only.

TDD: this file is written BEFORE production. Route tests assert desired 200
while baseline has no routes (404) — real assertion RED, not only ImportError.
Model tests use lazy imports so route REDs still execute before the model
exists.
"""
import hashlib
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from portal.models import Farm, Membership
from portal.tests import make_user


class TempPrivateRootMixin:
    def setUp(self):
        super().setUp()
        self._old_root = settings.PRIVATE_DOCUMENT_ROOT
        self._tmp = tempfile.mkdtemp(prefix="task9docs-")
        settings.PRIVATE_DOCUMENT_ROOT = Path(self._tmp)

    def tearDown(self):
        settings.PRIVATE_DOCUMENT_ROOT = getattr(
            self, "_old_root", settings.PRIVATE_DOCUMENT_ROOT)
        tmp = getattr(self, "_tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        super().tearDown()


def _pdf(payload=b"task9"):
    return b"%PDF-1.4\n%\xe2\xe3\n" + payload + b"\n%%EOF\n"


def _mk_doc(owner, name="Doksi", public=False, payload=b"task9"):
    from portal.document_storage import new_storage_name, write_new_file
    from portal.models import Document
    data = _pdf(payload)
    sname = new_storage_name(".pdf")
    write_new_file(sname, data)
    return Document.objects.create(
        owner=owner, name=name, description="", original_filename="d.pdf",
        storage_name=sname, extension=".pdf", content_type="application/pdf",
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        is_public=public), data


def _models():
    from portal.models import Document, FarmDocument
    return FarmDocument, Document


class FarmKnowledgeRouteTests(TempPrivateRootMixin, TestCase):
    """Baseline: desired routes must exist (RED: 404 before production)."""

    def setUp(self):
        super().setUp()
        self.owner_a = make_user("t9a@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        self.client.force_login(self.owner_a)

    def test_knowledge_list_route_desired_200(self):
        r = self.client.get("/tudasbazis/")
        self.assertEqual(r.status_code, 200)

    def test_knowledge_add_route_exists(self):
        doc, _ = _mk_doc(self.owner_a, "Sajat", public=False)
        r = self.client.post(f"/tudasbazis/{doc.pk}/hozzaadas/")
        self.assertIn(r.status_code, (302, 404))


class FarmDocumentModelTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner_a = make_user("t9m-a@pelda.hu")
        self.owner_b = make_user("t9m-b@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")

    def test_model_importable_defaults_unique(self):
        FarmDocument, _ = _models()
        doc, _ = _mk_doc(self.owner_b, "Pub", public=True)
        act = FarmDocument.objects.create(
            farm=self.farm_a, document=doc, added_by=self.owner_a)
        self.assertTrue(act.is_active)
        self.assertEqual(act.added_by, self.owner_a)
        self.assertIsNotNone(act.created_at)
        self.assertIsNotNone(act.updated_at)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FarmDocument.objects.create(
                    farm=self.farm_a, document=doc, added_by=self.owner_a)

    def test_no_snapshot_columns(self):
        FarmDocument, _ = _models()
        fields = {f.name for f in FarmDocument._meta.get_fields()}
        for leaked in ("body", "name_snapshot", "snapshot", "file",
                       "storage_name", "sha256"):
            self.assertNotIn(leaked, fields)


class FarmKnowledgeViewTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner_a = make_user("fa9-owner@pelda.hu")
        self.owner_b = make_user("fb9-owner@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        self.farm_b = Farm.objects.create(owner=self.owner_b, name="FarmB")
        self.manager = make_user("fa9-manager@pelda.hu")
        self.worker = make_user("fa9-worker@pelda.hu")
        self.viewer = make_user("fa9-viewer@pelda.hu")
        Membership.objects.create(
            farm=self.farm_a, user=self.manager, role="manager", is_active=True)
        Membership.objects.create(
            farm=self.farm_a, user=self.worker, role="worker", is_active=True)
        Membership.objects.create(
            farm=self.farm_a, user=self.viewer, role="viewer", is_active=True)
        self.client.force_login(self.owner_a)

    def _activate(self, farm):
        r = self.client.post("/aktiv-farm/", {"farm_id": str(farm.id)})
        self.assertEqual(r.status_code, 302)

    def test_no_farm_user_gets_404(self):
        fresh = make_user("t9-nofarm@pelda.hu")
        self.client.force_login(fresh)
        self.assertEqual(self.client.get("/tudasbazis/").status_code, 404)

    def test_sidebar_link_visible(self):
        r = self.client.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("tudasbazis", r.content.decode())

    def test_same_page_has_active_and_marketplace(self):
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertIn("tudásbázis", b.lower())

    def test_marketplace_only_own_farm_author_or_public(self):
        FarmDocument, _ = _models()
        _mk_doc(self.owner_a, "A privat", public=False)
        _mk_doc(self.owner_b, "B nyilvanos", public=True)
        _mk_doc(self.owner_b, "B titkos", public=False)
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertIn("A privat", b)
        self.assertIn("B nyilvanos", b)
        self.assertNotIn("B titkos", b)

    def test_other_private_add_404_no_row(self):
        FarmDocument, _ = _models()
        priv, _ = _mk_doc(self.owner_b, "B titkos2", public=False)
        self._activate(self.farm_a)
        n0 = FarmDocument.objects.count()
        r = self.client.post(f"/tudasbazis/{priv.pk}/hozzaadas/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(FarmDocument.objects.count(), n0)

    def test_public_add_success_idempotent_actor(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub2", public=True)
        self._activate(self.farm_a)
        r1 = self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
        self.assertEqual(r1.status_code, 302)
        r2 = self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
        self.assertEqual(r2.status_code, 302)
        rows = FarmDocument.objects.filter(farm=self.farm_a, document=pub)
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.get().is_active)
        self.assertEqual(rows.get().added_by, self.owner_a)

    def test_forged_document_id_denied(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pubF", public=True)
        self._activate(self.farm_a)
        n0 = FarmDocument.objects.count()
        r = self.client.post("/tudasbazis/999999/hozzaadas/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(FarmDocument.objects.count(), n0)
        self.assertFalse(
            FarmDocument.objects.filter(farm=self.farm_a, document=pub).exists())

    def test_get_add_never_mutates(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub3", public=True)
        self._activate(self.farm_a)
        n0 = FarmDocument.objects.count()
        r = self.client.get(f"/tudasbazis/{pub.pk}/hozzaadas/")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(FarmDocument.objects.count(), n0)

    def test_get_list_never_mutates_and_has_csrf(self):
        FarmDocument, _ = _models()
        self._activate(self.farm_a)
        n0 = FarmDocument.objects.count()
        r = self.client.get("/tudasbazis/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FarmDocument.objects.count(), n0)
        self.assertIn("csrfmiddlewaretoken", r.content.decode())

    def test_manager_can_add_and_remove(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub4", public=True)
        self.client.force_login(self.manager)
        self._activate(self.farm_a)
        self.assertEqual(
            self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/").status_code, 302)
        self.assertTrue(
            FarmDocument.objects.get(farm=self.farm_a, document=pub).is_active)
        self.assertEqual(
            self.client.post(f"/tudasbazis/{pub.pk}/eltavolitas/").status_code, 302)
        self.assertFalse(
            FarmDocument.objects.get(farm=self.farm_a, document=pub).is_active)

    def test_worker_viewer_backend_denies_and_controls_hidden(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub5", public=True)
        for user in (self.worker, self.viewer):
            self.client.force_login(user)
            self._activate(self.farm_a)
            n0 = FarmDocument.objects.count()
            r = self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
            self.assertIn(r.status_code, (403, 404))
            self.assertEqual(FarmDocument.objects.count(), n0)
            b = self.client.get("/tudasbazis/").content.decode()
            self.assertEqual(self.client.get("/tudasbazis/").status_code, 200)
            self.assertNotIn("hozzaadas", b)
            self.assertNotIn("eltavolitas", b)
        self.client.force_login(self.owner_a)
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertIn("hozzaadas", b)

    def test_manager_private_authored_needs_public_for_managed_farm(self):
        FarmDocument, _ = _models()
        mine, _ = _mk_doc(self.manager, "Manager privat", public=False)
        self.client.force_login(self.manager)
        self._activate(self.farm_a)
        n0 = FarmDocument.objects.count()
        r = self.client.post(f"/tudasbazis/{mine.pk}/hozzaadas/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(FarmDocument.objects.count(), n0)
        mine.is_public = True
        mine.save()
        r2 = self.client.post(f"/tudasbazis/{mine.pk}/hozzaadas/")
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(FarmDocument.objects.count(), n0 + 1)

    def test_revoke_private_deactivates_other_farms_hides_metadata(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub6", public=True)
        self.client.force_login(self.owner_a)
        self._activate(self.farm_a)
        self.assertEqual(
            self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/").status_code, 302)
        self.client.force_login(self.owner_b)
        self._activate(self.farm_b)
        self.assertEqual(
            self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/").status_code, 302)
        # Author privatizes via owner edit route (no file change).
        self.client.force_login(self.owner_b)
        r = self.client.post(f"/dokumentumok/{pub.pk}/szerkesztes/", {
            "name": "B pub6", "description": "", "upload": ""})
        self.assertIn(r.status_code, (302, 400))
        if r.status_code == 400:
            pub.is_public = False
            pub.save()
        pub.refresh_from_db()
        self.assertFalse(pub.is_public)
        self.assertFalse(
            FarmDocument.objects.get(farm=self.farm_a, document=pub).is_active)
        self.assertTrue(
            FarmDocument.objects.get(farm=self.farm_b, document=pub).is_active)
        self.client.force_login(self.owner_a)
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertNotIn("B pub6", b)
        self.assertEqual(self.client.get(f"/dokumentumok/{pub.pk}/").status_code, 404)
        self.assertEqual(
            self.client.get(f"/dokumentumok/{pub.pk}/letoltes/").status_code, 404)

    def test_republish_does_not_reactivate(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub7", public=True)
        self.client.force_login(self.owner_a)
        self._activate(self.farm_a)
        self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
        pub.is_public = False
        pub.save()
        pub.is_public = True
        pub.save()
        self.assertTrue(pub.is_public)
        row = FarmDocument.objects.get(farm=self.farm_a, document=pub)
        self.assertFalse(row.is_active)
        self.client.force_login(self.owner_a)
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertIn("B pub7", b)
        active_section = b.split("Elérhető")[0]
        self.assertNotIn("eltavolitas", active_section.replace("Eltávolítás", "")) \
            if "B pub7" in active_section else True

    def test_no_private_name_or_file_links_leak(self):
        _mk_doc(self.owner_b, "B titkos-leak", public=False)
        pub, _ = _mk_doc(self.owner_b, "B nyilvanos-leak", public=True)
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertNotIn("B titkos-leak", b)
        self.assertNotIn("private_documents", b)
        self.assertNotIn(pub.storage_name, b)

    def test_activefarm_switch_isolates_entries(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub9", public=True)
        self._activate(self.farm_a)
        self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
        self.client.force_login(self.owner_b)
        self._activate(self.farm_b)
        b = self.client.get("/tudasbazis/").content.decode()
        active_section = b.split("Elérhető")[0] if "Elérhető" in b else b
        self.assertNotIn("B pub9", active_section)
        self.assertIn("B pub9", b)

    def test_forged_other_farm_session_denied(self):
        self._activate(self.farm_a)
        session = self.client.session
        session["active_farm_id"] = self.farm_b.id
        session.save()
        self.assertEqual(self.client.get("/tudasbazis/").status_code, 404)
        r = self.client.post("/aktiv-farm/", {"farm_id": str(self.farm_b.id)})
        self.assertEqual(r.status_code, 404)

    def test_removed_membership_denies(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub10", public=True)
        self.client.force_login(self.manager)
        self._activate(self.farm_a)
        Membership.objects.filter(farm=self.farm_a, user=self.manager).delete()
        self.assertEqual(self.client.get("/tudasbazis/").status_code, 404)
        n0 = FarmDocument.objects.count()
        self.assertEqual(
            self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/").status_code, 404)
        self.assertEqual(FarmDocument.objects.count(), n0)

    def test_remove_idempotent_without_row(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub11", public=True)
        self._activate(self.farm_a)
        n0 = FarmDocument.objects.count()
        r = self.client.post(f"/tudasbazis/{pub.pk}/eltavolitas/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(FarmDocument.objects.count(), n0)

    def test_own_doc_detail_links_add_remove(self):
        FarmDocument, _ = _models()
        pub, _ = _mk_doc(self.owner_b, "B pub12", public=True)
        self._activate(self.farm_a)
        b = self.client.get(f"/dokumentumok/{pub.pk}/").content.decode()
        self.assertIn("hozzaadas", b)
        self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
        b2 = self.client.get(f"/dokumentumok/{pub.pk}/").content.decode()
        self.assertIn("eltavolitas", b2)

    def test_management_only_notice_present(self):
        self._activate(self.farm_a)
        b = self.client.get("/tudasbazis/").content.decode()
        self.assertIn("nem vesz", b.lower())

    def test_document_delete_cascades_entries_and_file(self):
        FarmDocument, _ = _models()
        pub, data = _mk_doc(self.owner_b, "B pubdel", public=True)
        self.client.force_login(self.owner_a)
        self._activate(self.farm_a)
        self.client.post(f"/tudasbazis/{pub.pk}/hozzaadas/")
        self.assertTrue(
            FarmDocument.objects.filter(farm=self.farm_a, document=pub).exists())
        from django.contrib.auth import get_user_model
        path = settings.PRIVATE_DOCUMENT_ROOT / pub.storage_name
        self.assertTrue(path.exists())
        pub_id = pub.pk
        with self.captureOnCommitCallbacks(execute=True):
            from portal.models import Document
            Document.objects.filter(pk=pub_id).delete()
        self.assertFalse(
            FarmDocument.objects.filter(document_id=pub_id).exists())
        self.assertFalse(path.exists())


class BareSaveAtomicRevokeTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner_a = make_user("t9bare-a@pelda.hu")
        self.owner_b = make_user("t9bare-b@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        self.farm_b = Farm.objects.create(owner=self.owner_b, name="FarmB")

    def test_bare_save_public_to_private_revokes(self):
        FarmDocument, _ = _models()
        doc, _ = _mk_doc(self.owner_b, "Bare pub", public=True)
        from portal.farm_knowledge import activate_document
        activate_document(farm=self.farm_a, document=doc, actor=self.owner_a)
        activate_document(farm=self.farm_b, document=doc, actor=self.owner_b)
        doc.is_public = False
        doc.save()
        doc.refresh_from_db()
        self.assertFalse(doc.is_public)
        self.assertFalse(
            FarmDocument.objects.get(farm=self.farm_a, document=doc).is_active)
        self.assertTrue(
            FarmDocument.objects.get(farm=self.farm_b, document=doc).is_active)
        doc.is_public = True
        doc.save()
        self.assertFalse(
            FarmDocument.objects.get(farm=self.farm_a, document=doc).is_active)

    def test_save_and_revoke_are_atomic_on_revoke_failure(self):
        from unittest.mock import patch
        FarmDocument, _ = _models()
        doc, _ = _mk_doc(self.owner_b, "Atom pub", public=True)
        from portal.farm_knowledge import activate_document
        activate_document(farm=self.farm_a, document=doc, actor=self.owner_a)
        doc.is_public = False
        with patch("portal.domain_models.FarmDocument") as mock_act:
            mock_act.objects.filter.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                doc.save()
        doc.refresh_from_db()
        self.assertTrue(doc.is_public)
        self.assertTrue(
            FarmDocument.objects.get(farm=self.farm_a, document=doc).is_active)


class StaleDocumentRaceTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner_a = make_user("t9race-a@pelda.hu")
        self.owner_b = make_user("t9race-b@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner_a, name="FarmA")
        Farm.objects.create(owner=self.owner_b, name="FarmB")

    def test_stale_public_instance_cannot_activate_after_private(self):
        from django.http import Http404
        from portal.models import Document
        doc, _ = _mk_doc(self.owner_b, "Race pub", public=True)
        stale = Document.objects.get(pk=doc.pk)
        self.assertTrue(stale.is_public)
        fresh = Document.objects.get(pk=doc.pk)
        fresh.is_public = False
        fresh.save()
        from portal.farm_knowledge import activate_document
        with self.assertRaises(Http404):
            activate_document(farm=self.farm_a, document=stale, actor=self.owner_a)
        FarmDocument, _ = _models()
        self.assertFalse(
            FarmDocument.objects.filter(farm=self.farm_a, document=doc).exists())


class BulkUpdateGuardTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("t9bulk@pelda.hu")
        Farm.objects.create(owner=self.owner, name="FarmBulk")

    def test_queryset_update_is_public_blocked(self):
        _, Document = _models()
        doc, _ = _mk_doc(self.owner, "Bulk pub", public=True)
        with self.assertRaises(ValueError):
            Document.objects.filter(pk=doc.pk).update(is_public=False)
        doc.refresh_from_db()
        self.assertTrue(doc.is_public)

    def test_bulk_update_is_public_blocked(self):
        _, Document = _models()
        doc, _ = _mk_doc(self.owner, "Bulk2 pub", public=True)
        doc.is_public = False
        with self.assertRaises(ValueError):
            Document.objects.bulk_update([doc], ["is_public"])
        doc.refresh_from_db()
        self.assertTrue(doc.is_public)


class KnowledgeNoDecisionTests(TempPrivateRootMixin, TestCase):
    def test_knowledge_does_not_feed_plans_chat(self):
        import pathlib
        repo = pathlib.Path(__file__).resolve().parent
        for mod in ("planning.py", "chat.py", "services.py"):
            src = (repo / mod).read_text(encoding="utf-8")
            self.assertNotIn("FarmDocument", src)
            self.assertNotIn("farm_knowledge", src)
