"""Second registry suite: remaining families with real DB CRUD."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from portal.agent import registry
from portal.agent.test_agent_registry import (
    _cycle,
    _farm,
    _location,
    _member,
    _property,
    _user,
    cmd,
    run,
)
from portal.test_documents import TempPrivateRootMixin

User = get_user_model()


def _equipment(farm, name="Traktor"):
    from portal.models import Equipment
    return Equipment.objects.create(
        farm=farm, name=name, capabilities=["tillage"],
        available=True, hours_per_day="8")


def _document(owner, name="Irat"):
    import uuid
    from portal.domain_models import Document
    return Document.objects.create(
        owner=owner, name=name, description="",
        original_filename="d.pdf",
        storage_name=uuid.uuid4().hex,
        extension="pdf", content_type="application/pdf",
        size_bytes=10, sha256="a" * 64, is_public=False)


class RemainingPositiveTest(TestCase):
    def setUp(self):
        self.owner = _user("r2-o@pelda.hu")
        self.farm = _farm(self.owner)

    def test_equipment_property_attach_update_delete(self):
        eq = _equipment(self.farm)
        prop = _property(self.farm)
        prop2 = _property(self.farm, name="Masik")
        res, _p = run(self.owner, self.farm, [
            cmd("equipment_property", "create",
                {"equipment": eq.pk, "property": prop.pk})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["equipment"], eq.pk)
        res, _p = run(self.owner, self.farm, [
            cmd("equipment_property", "update",
                {"property": prop2.pk}, obj_id=pk)])
        self.assertEqual(res[0]["after"]["property"], prop2.pk)
        res, _p = run(self.owner, self.farm, [
            cmd("equipment_property", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_empty_livestock_group_create_edit_archive(self):
        prop = _property(self.farm)
        res, _p = run(self.owner, self.farm, [
            cmd("livestock_group", "create", {
                "name": "Gulya", "species": "cattle",
                "breed_label": "", "headcount": 5,
                "housing_property": prop.pk, "notes": ""})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["headcount"], 5)
        res, _p = run(self.owner, self.farm, [
            cmd("livestock_group", "update", {"name": "Gulya2"},
                obj_id=pk)])
        self.assertEqual(res[0]["after"]["name"], "Gulya2")
        res, _p = run(self.owner, self.farm, [
            cmd("livestock_group", "update", {"is_active": False},
                obj_id=pk)])
        self.assertFalse(res[0]["after"]["is_active"])

    def test_news_source_crud_owner(self):
        res, _p = run(self.owner, self.farm, [
            cmd("news_source", "create", {
                "name": "Portal", "url": "https://example.com/feed",
                "notes": "", "is_active": True})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["name"], "Portal")
        res, _p = run(self.owner, self.farm, [
            cmd("news_source", "update", {"notes": "Friss"},
                obj_id=pk)])
        self.assertEqual(res[0]["after"]["notes"], "Friss")
        res, _p = run(self.owner, self.farm, [
            cmd("news_source", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_invitation_create_and_revoke(self):
        res, _p = run(self.owner, self.farm, [
            cmd("invitation", "create",
                {"email": "uj@pelda.hu", "role": "viewer"})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["email"], "uj@pelda.hu")
        res, _p = run(self.owner, self.farm, [
            cmd("invitation", "update", {"revoked": True},
                obj_id=pk)])
        self.assertIsNotNone(res[0]["after"])

    def test_membership_owner_update(self):
        member = _user("r2-m@pelda.hu")
        _member(self.farm, member, "manager")
        row = member.memberships.get(farm=self.farm)
        res, _p = run(self.owner, self.farm, [
            cmd("membership", "update", {"role": "worker"},
                obj_id=row.pk)])
        self.assertEqual(res[0]["after"]["role"], "worker")

    def test_farm_document_active_eligible_crud(self):
        doc = _document(self.owner)
        res, _p = run(self.owner, self.farm, [
            cmd("farm_document", "create", {"document": doc.pk})])
        pk = res[0]["pk"]
        self.assertTrue(res[0]["after"]["is_active"])
        res, _p = run(self.owner, self.farm, [
            cmd("farm_document", "update", {"is_active": False},
                obj_id=pk)])
        self.assertFalse(res[0]["after"]["is_active"])

    def test_farming_event_create_with_fact(self):
        prop = _property(self.farm)
        res, _p = run(self.owner, self.farm, [
            cmd("farming_event", "create", {
                "property": prop.pk, "event_date": "2026-03-01",
                "event_type": "other", "description": "Kapalas a tablan",
                "notes": "Kapaltam a tablan.",
                "fact_ack": True})])
        self.assertEqual(res[0]["after"]["description"], "Kapalas a tablan")
        from portal.domain_models import FarmingEvent
        row = FarmingEvent.objects.get(pk=res[0]["pk"])
        self.assertEqual(row.created_by_id, self.owner.pk)
        self.assertEqual(row.property_id, prop.pk)

    def test_document_owner_metadata_update(self):
        doc = _document(self.owner)
        res, _p = run(self.owner, self.farm, [
            cmd("document", "update", {"name": "Atnevezett"},
                obj_id=doc.pk)])
        self.assertEqual(res[0]["after"]["name"], "Atnevezett")
        doc.refresh_from_db()
        self.assertEqual(doc.name, "Atnevezett")


class RemainingNegativeTest(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = _user("r2n-o@pelda.hu")
        self.farm = _farm(self.owner)
        self.other = _user("r2n-x@pelda.hu")

    def test_equipment_property_cross_farm_rejected(self):
        eq = _equipment(self.farm)
        farm2 = _farm(self.other, name="Masik")
        prop2 = _property(farm2)
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [
                cmd("equipment_property", "create",
                    {"equipment": eq.pk, "property": prop2.pk})])

    def test_livestock_group_delete_blocked_with_event(self):
        prop = _property(self.farm)
        res, _p = run(self.owner, self.farm, [
            cmd("livestock_group", "create", {
                "name": "Gulya", "species": "cattle",
                "breed_label": "", "headcount": 5,
                "housing_property": prop.pk, "notes": ""})])
        pk = res[0]["pk"]
        run(self.owner, self.farm, [
            cmd("livestock_event", "create", {
                "group": pk, "event_date": "2026-01-05",
                "kind": "health", "headcount_delta": 0,
                "fact_ack": True, "fact_statement": "Vizsgalat volt."})])
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm,
                [cmd("livestock_group", "delete", obj_id=pk)])

    def test_news_source_cross_user_isolated_no_owner_fallback(self):
        res, _p = run(self.owner, self.farm, [
            cmd("news_source", "create", {
                "name": "Sajat", "url": "https://example.com/a",
                "notes": "", "is_active": True})])
        pk = res[0]["pk"]
        seen_owner = registry.snapshot_farm(
            self.farm, actor=self.owner)["news_source"]
        self.assertTrue(any(r["id"] == pk for r in seen_owner))
        seen_other = registry.snapshot_farm(
            self.farm, actor=self.other)["news_source"]
        self.assertFalse(any(r["id"] == pk for r in seen_other))
        self.assertIsNone(
            registry.snapshot_one(
                self.farm, "news_source", pk, actor=self.other))

    def test_invitation_delete_blocked(self):
        res, _p = run(self.owner, self.farm, [
            cmd("invitation", "create",
                {"email": "torol@pelda.hu", "role": "viewer"})])
        with self.assertRaisesRegex(ValidationError, "revoke"):
            run(self.owner, self.farm, [
                cmd("invitation", "delete", obj_id=res[0]["pk"])])

    def test_membership_manager_cannot_update(self):
        manager = _user("r2n-m@pelda.hu")
        _member(self.farm, manager, "manager")
        worker = _user("r2n-w@pelda.hu")
        _member(self.farm, worker, "worker")
        row = worker.memberships.get(farm=self.farm)
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(manager, self.farm, [
                cmd("membership", "update", {"role": "viewer"},
                    obj_id=row.pk)])

    def test_farm_document_ineligible_blocked(self):
        doc = _document(self.other)
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [
                cmd("farm_document", "create", {"document": doc.pk})])

    def test_farming_event_without_fact_ack_blocked(self):
        prop = _property(self.farm)
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [
                cmd("farming_event", "create", {
                    "property": prop.pk, "event_date": "2026-03-01",
                    "event_type": "other", "description": "Kapas"})])

    def test_cycle_alias_and_document_create_blocked(self):
        parsed = registry.parse_command({
            "target": "cycle", "op": "create",
            "fields": {"property": 1, "crop_name": "Buza"}})
        self.assertEqual(parsed["target"], "crop_cycle")
        with self.assertRaisesRegex(ValidationError, "not offered"):
            run(self.owner, self.farm, [
                cmd("document", "create", {"name": "Uj"})])
        with self.assertRaises(Exception):
            registry.parse_command({
                "target": "document", "op": "create",
                "fields": {"name": "Uj", "upload": "x"}})
