"""RED/GREEN tests for the ten explicit registry adapters.

Each adapter gets a positive create/update/delete path for the owning
role plus negative paths: cross-farm references, wrong roles, unknown
fields and protected history blocks. Validation runs through the real
forms and model full_clean; execution writes for real in isolation.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from portal.agent import registry
from portal.models import Farm, Membership

User = get_user_model()


def _user(email):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(owner, name="Tesztfarm"):
    return Farm.objects.create(owner=owner, name=name)


def _member(farm, user, role):
    return Membership.objects.create(
        farm=farm, user=user, role=role, is_active=True)


def _location(farm, name="Kozpont"):
    from portal.models import FarmLocation
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude="46.250000",
        longitude="20.150000", timezone="Europe/Budapest")


def _property(farm, location=None, name="Tabla"):
    from portal.models import FarmProperty
    location = location or _location(farm)
    return FarmProperty.objects.create(
        farm=farm, location=location, name=name, kind="field",
        area_hectares="5.00")


def _cycle(farm, prop=None, crop="Buza", state="current"):
    from portal.models import CropCycle
    prop = prop or _property(farm)
    return CropCycle.objects.create(
        farm=farm, property=prop, crop_name=crop, state=state)


def run(actor, farm, commands):
    parsed = registry.validate_commands(actor, farm, commands)
    return registry.execute_commands(farm, parsed, actor=actor), parsed


def cmd(target, op, fields=None, obj_id=None, ref=""):
    out = {"target": target, "op": op, "fields": fields or {}}
    if obj_id is not None:
        out["id"] = obj_id
    if ref:
        out["ref"] = ref
    return out


class FarmAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("farm-o@pelda.hu")
        self.manager = _user("farm-m@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.manager, "manager")
        self.other = _user("farm-x@pelda.hu")
        self.farm2 = _farm(self.other, name="Masik")

    def test_update_own_farm(self):
        res, _p = run(self.owner, self.farm, [
            cmd("farm", "update", {"name": "Atnevezett"},
                obj_id=self.farm.pk)])
        self.assertEqual(res[0]["after"]["name"], "Atnevezett")
        self.farm.refresh_from_db()
        self.assertEqual(self.farm.name, "Atnevezett")

    def test_manager_cannot_update_farm(self):
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(self.manager, self.farm, [
                cmd("farm", "update", {"name": "X"},
                    obj_id=self.farm.pk)])

    def test_create_blocked(self):
        with self.assertRaisesRegex(ValidationError, "not offered"):
            run(self.owner, self.farm, [cmd("farm", "create", {"name": "Uj"})])

    def test_delete_blocked_with_history(self):
        from portal.models import FarmAction
        FarmAction.objects.create(farm=self.farm, text="Teendo")
        with self.assertRaisesRegex(ValidationError, "account deletion"):
            run(self.owner, self.farm,
                [cmd("farm", "delete", obj_id=self.farm.pk)])

    def test_update_other_farm_id_rejected(self):
        with self.assertRaisesRegex(ValidationError, "not the active farm"):
            run(self.owner, self.farm, [
                cmd("farm", "update", {"name": "X"},
                    obj_id=self.farm2.pk)])


class ExtraFieldAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("ex-o@pelda.hu")
        self.farm = _farm(self.owner)
        self.outsider = _user("ex-x@pelda.hu")

    def test_full_crud(self):
        res, _p = run(self.owner, self.farm, [
            cmd("extra_field", "create", {"key": "Ontozes", "value": "Van"})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["key"], "Ontozes")
        res, _p = run(self.owner, self.farm, [
            cmd("extra_field", "update", {"value": "Nincs"}, obj_id=pk)])
        self.assertEqual(res[0]["after"]["value"], "Nincs")
        res, _p = run(self.owner, self.farm,
                      [cmd("extra_field", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_duplicate_key_rejected(self):
        run(self.owner, self.farm, [
            cmd("extra_field", "create", {"key": "Talaj", "value": "A"})])
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [
                cmd("extra_field", "create",
                    {"key": "  talaj ", "value": "B"})])

    def test_unknown_field_rejected(self):
        with self.assertRaisesRegex(Exception, "Unknown extra_field"):
            registry.parse_command(
                {"target": "extra_field", "op": "create",
                 "fields": {"key": "K", "value": "V", "position": 3}})

    def test_outsider_blocked(self):
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(self.outsider, self.farm, [
                cmd("extra_field", "create",
                    {"key": "K", "value": "V"})])


class LocationAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("loc-o@pelda.hu")
        self.viewer = _user("loc-v@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.viewer, "viewer")

    def test_full_crud(self):
        res, _p = run(self.owner, self.farm, [cmd("location", "create", {
            "name": "Dulo", "latitude": "46.250000",
            "longitude": "20.150000", "timezone": "Europe/Budapest"})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["timezone"], "Europe/Budapest")
        res, _p = run(self.owner, self.farm, [
            cmd("location", "update", {"name": "Dulo2"}, obj_id=pk)])
        self.assertEqual(res[0]["after"]["name"], "Dulo2")
        res, _p = run(self.owner, self.farm,
                      [cmd("location", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [cmd("location", "create", {
                "name": "Rossz", "latitude": "46.25", "longitude": "20.15",
                "timezone": "Mars/Olympus"})])

    def test_delete_blocked_with_property(self):
        loc = _location(self.farm)
        _property(self.farm, location=loc)
        with self.assertRaisesRegex(ValidationError, "property rows"):
            run(self.owner, self.farm,
                [cmd("location", "delete", obj_id=loc.pk)])

    def test_viewer_blocked(self):
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(self.viewer, self.farm, [
                cmd("location", "create", {
                    "name": "D", "latitude": "46.25", "longitude": "20.15",
                    "timezone": "Europe/Budapest"})])


class PropertyAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("prop-o@pelda.hu")
        self.worker = _user("prop-w@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.worker, "worker")
        self.other = _user("prop-x@pelda.hu")
        self.farm2 = _farm(self.other, name="Masik")
        self.loc = _location(self.farm)

    def test_full_crud(self):
        res, _p = run(self.owner, self.farm, [cmd("property", "create", {
            "location": self.loc.pk, "name": "Tabla1", "kind": "field",
            "area_hectares": "5.00"})])
        pk = res[0]["pk"]
        self.assertEqual(res[0]["after"]["area_hectares"], "5.00")
        res, _p = run(self.owner, self.farm, [
            cmd("property", "update", {"notes": "Jegyzet"}, obj_id=pk)])
        self.assertEqual(res[0]["after"]["notes"], "Jegyzet")
        res, _p = run(self.owner, self.farm,
                      [cmd("property", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_cross_farm_location_rejected(self):
        loc2 = _location(self.farm2, name="Idegen")
        with self.assertRaisesRegex(ValidationError, "Cross-farm"):
            run(self.owner, self.farm, [cmd("property", "create", {
                "location": loc2.pk, "name": "TablaX", "kind": "field",
                "area_hectares": "1.00"})])

    def test_delete_blocked_with_cycle(self):
        prop = _property(self.farm, location=self.loc)
        _cycle(self.farm, prop=prop)
        with self.assertRaisesRegex(ValidationError, "protected history"):
            run(self.owner, self.farm,
                [cmd("property", "delete", obj_id=prop.pk)])

    def test_worker_blocked(self):
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(self.worker, self.farm, [
                cmd("property", "create", {
                    "location": self.loc.pk, "name": "T", "kind": "field",
                    "area_hectares": "1.00"})])


class CropCycleAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("cyc-o@pelda.hu")
        self.farm = _farm(self.owner)
        self.other = _user("cyc-x@pelda.hu")
        self.farm2 = _farm(self.other, name="Masik")
        self.prop = _property(self.farm)

    def test_create_is_planned_and_update(self):
        res, _p = run(self.owner, self.farm, [cmd("crop_cycle", "create", {
            "property": self.prop.pk, "crop_name": "Kukorica",
            "sow_date": "2026-03-01", "expected_end_date": "2026-09-01"})])
        self.assertEqual(res[0]["after"]["state"], "planned")
        pk = res[0]["pk"]
        res, _p = run(self.owner, self.farm, [
            cmd("crop_cycle", "update", {"crop_name": "Napraforgo"},
                obj_id=pk)])
        self.assertEqual(res[0]["after"]["crop_name"], "Napraforgo")

    def test_state_key_rejected(self):
        with self.assertRaisesRegex(Exception, "Unknown crop_cycle"):
            registry.parse_command(
                {"target": "crop_cycle", "op": "create",
                 "fields": {"property": 1, "crop_name": "X",
                            "state": "current"}})

    def test_cross_farm_property_rejected(self):
        prop2 = _property(self.farm2)
        with self.assertRaisesRegex(ValidationError, "Cross-farm"):
            run(self.owner, self.farm, [cmd("crop_cycle", "create", {
                "property": prop2.pk, "crop_name": "Buza",
                "sow_date": "2026-03-01"})])

    def test_delete_blocked_with_event(self):
        from portal.models import FarmingEvent
        cyc = _cycle(self.farm, prop=self.prop, state="planned")
        FarmingEvent.objects.create(
            farm=self.farm, property=self.prop, crop_cycle=cyc,
            event_date="2026-04-01", event_type="other",
            description="Permetezes")
        with self.assertRaisesRegex(ValidationError,
                                    "protected history"):
            run(self.owner, self.farm,
                [cmd("crop_cycle", "delete", obj_id=cyc.pk)])

    def test_delete_without_history_ok(self):
        res, _p = run(self.owner, self.farm, [cmd("crop_cycle", "create", {
            "property": self.prop.pk, "crop_name": "Arpa",
            "sow_date": "2026-03-01"})])
        pk = res[0]["pk"]
        res, _p = run(self.owner, self.farm,
                      [cmd("crop_cycle", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])


class ObservationAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("obs-o@pelda.hu")
        self.worker = _user("obs-w@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.worker, "worker")
        self.other = _user("obs-x@pelda.hu")
        self.farm2 = _farm(self.other, name="Masik")
        self.cyc = _cycle(self.farm)

    def test_update_and_clear(self):
        res, _p = run(self.owner, self.farm, [
            cmd("observation", "update", {"observation": "Szep allomany"},
                obj_id=self.cyc.pk)])
        self.assertEqual(res[0]["after"]["observations"], "Szep allomany")
        res, _p = run(self.owner, self.farm, [
            cmd("observation", "update", {"observation": ""},
                obj_id=self.cyc.pk)])
        self.assertEqual(res[0]["after"]["observations"], "")

    def test_worker_may_observe(self):
        res, _p = run(self.worker, self.farm, [
            cmd("observation", "update", {"observation": "Munkas eszleles"},
                obj_id=self.cyc.pk)])
        self.assertEqual(res[0]["after"]["observations"],
                         "Munkas eszleles")

    def test_create_and_delete_blocked(self):
        with self.assertRaisesRegex(ValidationError, "not offered"):
            run(self.owner, self.farm, [
                cmd("observation", "create", {"observation": "X"})])
        with self.assertRaisesRegex(ValidationError, "empty observation"):
            run(self.owner, self.farm, [
                cmd("observation", "delete", obj_id=self.cyc.pk)])

    def test_cross_farm_cycle_rejected(self):
        cyc2 = _cycle(self.farm2)
        with self.assertRaisesRegex(ValidationError, "not available"):
            run(self.owner, self.farm, [
                cmd("observation", "update", {"observation": "X"},
                    obj_id=cyc2.pk)])


class ContactAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("con-o@pelda.hu")
        self.manager = _user("con-m@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.manager, "manager")

    def test_owner_full_crud(self):
        res, _p = run(self.owner, self.farm, [cmd("external_contact",
                                                 "create", {
            "name": "Bergep", "phone": "+36301234567",
            "services": "Szantas\nVetes"})])
        pk = res[0]["pk"]
        res, _p = run(self.owner, self.farm, [
            cmd("external_contact", "update",
                {"availability": "Hetfonkent"}, obj_id=pk)])
        self.assertEqual(res[0]["after"]["availability"], "Hetfonkent")
        res, _p = run(self.owner, self.farm, [
            cmd("external_contact", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_manager_blocked(self):
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(self.manager, self.farm, [
                cmd("external_contact", "create", {
                    "name": "X", "phone": "+36301112222",
                    "services": "Munka"})])

    def test_contact_needs_phone_or_email(self):
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [cmd("external_contact", "create", {
                "name": "Nevenincs", "services": "Munka"})])


class RuleCategoryAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("rc-o@pelda.hu")
        self.other = _user("rc-x@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.other, "viewer")

    def test_full_crud(self):
        res, _p = run(self.owner, self.farm, [
            cmd("rule_category", "create", {"name": "Talaj"})])
        pk = res[0]["pk"]
        res, _p = run(self.owner, self.farm, [
            cmd("rule_category", "update", {"name": "Talajelet"},
                obj_id=pk)])
        self.assertEqual(res[0]["after"]["name"], "Talajelet")
        res, _p = run(self.owner, self.farm, [
            cmd("rule_category", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_duplicate_name_rejected(self):
        run(self.owner, self.farm, [
            cmd("rule_category", "create", {"name": "Viz"})])
        with self.assertRaises(ValidationError):
            run(self.owner, self.farm, [
                cmd("rule_category", "create", {"name": "Viz"})])

    def test_same_name_for_other_owner_allowed(self):
        from portal.models import RuleCategory
        run(self.owner, self.farm, [
            cmd("rule_category", "create", {"name": "Kozos"})])
        run(self.other, self.farm, [
            cmd("rule_category", "create", {"name": "Kozos"})])
        self.assertEqual(
            RuleCategory.objects.filter(name="Kozos").count(), 2)

    def test_other_owner_row_not_available(self):
        res, _p = run(self.owner, self.farm, [
            cmd("rule_category", "create", {"name": "Sajat"})])
        pk = res[0]["pk"]
        with self.assertRaisesRegex(ValidationError, "not available"):
            run(self.other, self.farm, [
                cmd("rule_category", "update", {"name": "Atirt"},
                    obj_id=pk)])


class RuleDefinitionAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("rd-o@pelda.hu")
        self.other = _user("rd-x@pelda.hu")
        self.farm = _farm(self.owner)
        self.farm2 = _farm(self.other, name="Masik")
        from portal.models import RuleCategory
        self.cat = RuleCategory.objects.create(
            owner=self.owner, name="Talaj")

    def _create(self, **fields):
        base = {"name": "Szabaly", "category": self.cat.pk,
                "body": "Leiras"}
        base.update(fields)
        res, _p = run(self.owner, self.farm,
                      [cmd("rule_definition", "create", base)])
        return res[0]["pk"]

    def test_full_crud(self):
        pk = self._create()
        res, _p = run(self.owner, self.farm, [
            cmd("rule_definition", "update", {"body": "Uj leiras"},
                obj_id=pk)])
        self.assertEqual(res[0]["after"]["category"], self.cat.pk)
        res, _p = run(self.owner, self.farm, [
            cmd("rule_definition", "delete", obj_id=pk)])
        self.assertIsNone(res[0]["after"])

    def test_create_with_new_category(self):
        pk = self._create(new_category="FrissKat", category=None)
        from portal.models import RuleDefinition
        obj = RuleDefinition.objects.get(pk=pk)
        self.assertEqual(obj.category.name, "FrissKat")
        self.assertEqual(obj.category.owner, self.owner)

    def test_cross_user_category_rejected(self):
        from portal.models import RuleCategory
        cat2 = RuleCategory.objects.create(owner=self.other, name="Idegen")
        with self.assertRaisesRegex(ValidationError, "not yours"):
            run(self.owner, self.farm, [cmd("rule_definition", "create", {
                "name": "X", "category": cat2.pk, "body": "B"})])

    def test_public_revoke_deactivates_other_farm(self):
        from portal.models import RuleActivation
        pk = self._create(is_public=True)
        res, _p = run(self.other, self.farm2, [
            cmd("rule_activation", "create", {"rule": pk})])
        act_id = res[0]["pk"]
        self.assertTrue(RuleActivation.objects.get(pk=act_id).is_active)
        run(self.owner, self.farm, [
            cmd("rule_definition", "update", {"is_public": False},
                obj_id=pk)])
        self.assertFalse(
            RuleActivation.objects.get(pk=act_id).is_active)


class RuleActivationAdapterTest(TestCase):
    def setUp(self):
        self.owner = _user("ra-o@pelda.hu")
        self.manager = _user("ra-m@pelda.hu")
        self.worker = _user("ra-w@pelda.hu")
        self.farm = _farm(self.owner)
        _member(self.farm, self.manager, "manager")
        _member(self.farm, self.worker, "worker")
        self.other = _user("ra-x@pelda.hu")
        self.farm2 = _farm(self.other, name="Masik")
        from portal.models import RuleCategory, RuleDefinition
        cat = RuleCategory.objects.create(owner=self.owner, name="Kat")
        self.rule = RuleDefinition.objects.create(
            owner=self.owner, category=cat, name="S1", body="B")
        self.pub = RuleDefinition.objects.create(
            owner=self.other, name="Nylv", body="B", is_public=True)
        cat2 = RuleCategory.objects.create(owner=self.other, name="K2")
        self.priv = RuleDefinition.objects.create(
            owner=self.other, category=cat2, name="Titkos", body="B")

    def test_create_own_and_public(self):
        res, _p = run(self.owner, self.farm, [
            cmd("rule_activation", "create", {"rule": self.rule.pk})])
        self.assertEqual(res[0]["after"]["activated_by"], self.owner.pk)
        res, _p = run(self.manager, self.farm, [
            cmd("rule_activation", "create", {"rule": self.pub.pk})])
        self.assertEqual(res[0]["after"]["activated_by"],
                         self.manager.pk)

    def test_ineligible_private_rule_rejected(self):
        with self.assertRaisesRegex(ValidationError, "not eligible"):
            run(self.owner, self.farm, [
                cmd("rule_activation", "create", {"rule": self.priv.pk})])

    def test_duplicate_activation_rejected(self):
        run(self.owner, self.farm, [
            cmd("rule_activation", "create", {"rule": self.rule.pk})])
        with self.assertRaisesRegex(ValidationError, "already installed"):
            run(self.owner, self.farm, [
                cmd("rule_activation", "create", {"rule": self.rule.pk})])

    def test_rule_swap_rejected(self):
        res, _p = run(self.owner, self.farm, [
            cmd("rule_activation", "create", {"rule": self.rule.pk})])
        pk = res[0]["pk"]
        with self.assertRaisesRegex(ValidationError, "cannot be swapped"):
            run(self.owner, self.farm, [
                cmd("rule_activation", "update", {"rule": self.pub.pk},
                    obj_id=pk)])
        res, _p = run(self.owner, self.farm, [
            cmd("rule_activation", "update", {"is_active": False},
                obj_id=pk)])
        self.assertFalse(res[0]["after"]["is_active"])

    def test_actor_field_not_trusted(self):
        with self.assertRaisesRegex(Exception, "Unknown rule_activation"):
            registry.parse_command(
                {"target": "rule_activation", "op": "create",
                 "fields": {"rule": 1, "activated_by": 999}})

    def test_worker_blocked_and_delete_ok(self):
        with self.assertRaises(PermissionDenied):
            registry.validate_commands(self.worker, self.farm, [
                cmd("rule_activation", "create", {"rule": self.rule.pk})])
        res, _p = run(self.owner, self.farm, [
            cmd("rule_activation", "create", {"rule": self.rule.pk})])
        res, _p = run(self.owner, self.farm, [
            cmd("rule_activation", "delete", obj_id=res[0]["pk"])])
        self.assertIsNone(res[0]["after"])


class SnapshotAdapterTest(TestCase):
    def test_snapshot_has_sorted_scoped_rows(self):
        owner = _user("sn-o@pelda.hu")
        other = _user("sn-x@pelda.hu")
        farm = _farm(owner)
        _member(farm, other, "viewer")
        loc2 = _location(farm, name="B-hely")
        loc1 = _location(farm, name="A-hely")
        run(owner, farm, [cmd("rule_category", "create", {"name": "Enyem"})])
        run(other, farm, [cmd("rule_category", "create", {"name": "Mas"})])
        snap = registry.snapshot_farm(farm, actor=owner)
        for target in ("farm", "extra_field", "location", "property",
                       "crop_cycle", "observation", "external_contact",
                       "rule_category", "rule_definition",
                       "rule_activation"):
            self.assertIn(target, snap)
        locs = snap["location"]
        self.assertEqual([r["id"] for r in locs], sorted(r["id"] for r in locs))
        self.assertEqual([r["name"] for r in snap["rule_category"]],
                         ["Enyem"])
        snap2 = registry.snapshot_farm(farm, actor=other)
        self.assertEqual([r["name"] for r in snap2["rule_category"]],
                         ["Mas"])
        self.assertEqual(snap["action"], [])
        self.assertLess(loc2.pk, loc1.pk)
