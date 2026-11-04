"""Task 1 RED: user-defined farm extra label/value fields on the farm page.

RED-first: none of these helpers import the new model at module level, so a
missing implementation surfaces as assertion FAILs (not import/lookup ERRORs).
Prefix contract under test: inline formset ``extras`` on ``/gazdasagom/``.
"""
from django.apps import apps
from django.test import TestCase, Client
from django.contrib.auth import get_user_model

from .models import Farm, FarmProfileSnapshot, ConsentEvent

User = get_user_model()


def extra_model():
    try:
        return apps.get_model("portal", "FarmExtraField")
    except LookupError:
        return None


CORE = {
    "name": "Tesztfarm",
    "region_text": "",
    "climate_text": "",
    "farming_direction_text": "",
    "soil_notes": "",
    "water_notes": "",
    "crops_livestock_notes": "",
    "equipment_labour_notes": "",
    "goals_notes": "",
    "constraints_notes": "",
}


def extras_post(rows, initial=0):
    """Build the ``extras`` inline-formset management POST for given rows.

    Each row: dict with optional keys: id, key, value, delete (bool).
    """
    data = dict(CORE)
    data.update({
        "extras-TOTAL_FORMS": str(len(rows)),
        "extras-INITIAL_FORMS": str(initial),
        "extras-MIN_NUM_FORMS": "0",
        "extras-MAX_NUM_FORMS": "20",
    })
    for i, row in enumerate(rows):
        p = f"extras-{i}-"
        data[p + "key"] = row.get("key", "")
        data[p + "value"] = row.get("value", "")
        if row.get("id") is not None:
            data[p + "id"] = str(row["id"])
        if row.get("delete"):
            data[p + "DELETE"] = "on"
    return data


def make_owner(email="t1owner@pelda.hu"):
    u = User.objects.create_user(email=email, password="TesztJelszo123!")
    farm = Farm.objects.create(owner=u, name="Tesztfarm")
    c = Client()
    c.force_login(u)
    return u, farm, c


class FarmExtraFieldTests(TestCase):
    def test_model_registered_with_limits(self):
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        key_f = M._meta.get_field("key")
        val_f = M._meta.get_field("value")
        self.assertEqual(key_f.max_length, 64)
        self.assertEqual(val_f.max_length, 500)

    def test_owner_create_extra_via_farm_page(self):
        u, farm, c = make_owner()
        r = c.post("/gazdasagom/", extras_post([{"key": "Talaj pH", "value": "6.5"}]))
        self.assertEqual(r.status_code, 302)
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        self.assertTrue(
            M.objects.filter(farm=farm, key="Talaj pH", value="6.5").exists(),
            "extra field was not saved via the farm page",
        )
        farm.refresh_from_db()
        self.assertEqual(farm.profile_version, 2)

    def test_key_max_64_and_value_max_500_enforced(self):
        u, farm, c = make_owner()
        r = c.post("/gazdasagom/", extras_post([{"key": "k" * 65, "value": "ok"}]))
        self.assertEqual(r.status_code, 200, "overlong key must re-render the form with an error")
        r = c.post("/gazdasagom/", extras_post([{"key": "ok", "value": "v" * 501}]))
        self.assertEqual(r.status_code, 200, "overlong value must re-render the form with an error")
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        self.assertEqual(M.objects.filter(farm=farm).count(), 0)
        farm.refresh_from_db()
        self.assertEqual(farm.profile_version, 1)

    def test_max_20_fields_enforced(self):
        u, farm, c = make_owner()
        rows = [{"key": f"mezo {i}", "value": "x"} for i in range(21)]
        r = c.post("/gazdasagom/", extras_post(rows))
        self.assertEqual(r.status_code, 200, "21st field must be rejected")
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        self.assertEqual(M.objects.filter(farm=farm).count(), 0)

    def test_duplicate_labels_case_insensitive_rejected_friendly(self):
        u, farm, c = make_owner()
        rows = [{"key": "Öntözés", "value": "a"}, {"key": "  öntözés ", "value": "b"}]
        r = c.post("/gazdasagom/", extras_post(rows))
        self.assertEqual(r.status_code, 200, "duplicate labels must re-render with a friendly error")
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        self.assertEqual(M.objects.filter(farm=farm).count(), 0)
        farm.refresh_from_db()
        self.assertEqual(farm.profile_version, 1)

    def test_delete_extra_via_formset(self):
        u, farm, c = make_owner()
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        ex = M.objects.create(farm=farm, key="Törlendő", value="x")
        r = c.post("/gazdasagom/", extras_post(
            [{"id": ex.pk, "key": "Törlendő", "value": "x", "delete": True}], initial=1))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(M.objects.filter(pk=ex.pk).exists(), "extra field was not deleted")
        farm.refresh_from_db()
        self.assertEqual(farm.profile_version, 2)

    def test_version_bumps_once_on_combined_core_plus_extras_edit(self):
        u, farm, c = make_owner()
        data = extras_post([{"key": "Új mező", "value": "új érték"}])
        data["soil_notes"] = "Friss talajleírás"
        r = c.post("/gazdasagom/", data)
        self.assertEqual(r.status_code, 302)
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        self.assertTrue(M.objects.filter(farm=farm, key="Új mező").exists())
        farm.refresh_from_db()
        self.assertEqual(farm.soil_notes, "Friss talajleírás")
        self.assertEqual(farm.profile_version, 2, "combined core+extras mutation must bump version exactly once")

    def test_snapshot_carries_extras_and_old_snapshot_untouched(self):
        from .services import reserve_advice_run, execute_reserved_run

        class _Client:
            def __init__(self):
                self.calls = 0

            def generate_advice(self, profile, language=None):
                self.calls += 1
                steps = [{"title": f"L{i}", "action_text": f"A{i}",
                          "rationale": f"R{i}", "assumptions": [],
                          "risk_note": f"K{i}"} for i in (1, 2, 3)]
                return {"steps": steps, "assumptions": [],
                        "focused_questions": [], "is_general": False}

        u, farm, c = make_owner()
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        ConsentEvent.objects.create(farm=farm, actor=u, event_type="granted")
        run1 = reserve_advice_run(u, farm, "t1-idem-1")
        old_json = dict(run1.profile_snapshot.profile_json)
        # One pending run per farm is intentional: a distinct-key request
        # while run1 is pending returns run1, so complete the initial
        # legacy-snapshot run before requesting the new snapshot.
        completed1 = execute_reserved_run(run1, client=_Client())
        self.assertEqual(completed1.status, "success")
        c.post("/gazdasagom/", extras_post([{"key": "Kút", "value": "fúrt"}]))
        run2 = reserve_advice_run(u, farm, "t1-idem-2")
        self.assertIn("extras", run2.profile_snapshot.profile_json,
                      "new snapshot must compose the extras key")
        self.assertEqual(run2.profile_snapshot.profile_json["extras"],
                         [{"key": "Kút", "value": "fúrt"}])
        run1.profile_snapshot.refresh_from_db()
        self.assertEqual(run1.profile_snapshot.profile_json, old_json,
                         "old immutable snapshot must never be altered")
        self.assertNotIn("extras", run1.profile_snapshot.profile_json)

    def test_tampered_other_farm_extra_id_ignored(self):
        u, farm, c = make_owner()
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        other_u = User.objects.create_user(email="t1other@pelda.hu", password="TesztJelszo123!")
        other_farm = Farm.objects.create(owner=other_u, name="Másik")
        victim = M.objects.create(farm=other_farm, key="Titok", value="eredeti")
        r = c.post("/gazdasagom/", extras_post(
            [{"id": victim.pk, "key": "Titok", "value": "feltört"}], initial=0))
        victim.refresh_from_db()
        self.assertEqual(victim.value, "eredeti", "other farm's extra must be untouched")
        self.assertEqual(victim.farm_id, other_farm.pk)
        self.assertEqual(M.objects.filter(farm=farm).count(), 0)

    def test_roles_write_owner_manager_only_read_all(self):
        from .models import Membership
        u, farm, _ = make_owner()
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        M.objects.create(farm=farm, key="Olvasható", value="igen")
        worker = User.objects.create_user(email="t1worker@pelda.hu", password="TesztJelszo123!")
        Membership.objects.create(farm=farm, user=worker, role="worker", is_active=True)
        wc = Client()
        wc.force_login(worker)
        self.assertEqual(wc.get("/gazdasagom/").status_code, 200)
        self.assertContains(wc.get("/gazdasagom/"), "Olvasható")
        self.assertEqual(
            wc.post("/gazdasagom/", extras_post([{"key": "x", "value": "y"}])).status_code,
            403, "worker must not write extras",
        )
        mgr = User.objects.create_user(email="t1mgr@pelda.hu", password="TesztJelszo123!")
        Membership.objects.create(farm=farm, user=mgr, role="manager", is_active=True)
        mc = Client()
        mc.force_login(mgr)
        r = mc.post("/gazdasagom/", extras_post([{"key": "Menedzser mező", "value": "ok"}]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(M.objects.filter(farm=farm, key="Menedzser mező").exists())

    def test_xss_value_escaped_on_farm_page(self):
        u, farm, c = make_owner()
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        c.post("/gazdasagom/", extras_post([{"key": "Megjegyzés", "value": "<script>alert(1)</script>"}]))
        r = c.get("/gazdasagom/")
        self.assertContains(r, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(r, "<script>alert(1)</script>")

    def test_export_owner_scoped_includes_ordered_extras(self):
        import json
        u, farm, c = make_owner()
        M = extra_model()
        self.assertIsNotNone(M, "FarmExtraField model missing")
        M.objects.create(farm=farm, key="B mező", value="2")
        M.objects.create(farm=farm, key="A mező", value="1")
        r = c.get("/fiok/export/")
        self.assertEqual(r.status_code, 200)
        payload = json.loads(r.content.decode("utf-8"))
        self.assertIn("extras", payload)
        self.assertEqual(payload["extras"],
                         [{"key": "B mező", "value": "2"}, {"key": "A mező", "value": "1"}],
                         "export must carry the ordered extras list")
        other = User.objects.create_user(email="t1stranger@pelda.hu", password="TesztJelszo123!")
        Farm.objects.create(owner=other, name="Idegen")
        oc = Client()
        oc.force_login(other)
        opayload = json.loads(oc.get("/fiok/export/").content.decode("utf-8"))
        self.assertNotIn("Titok", json.dumps(opayload, ensure_ascii=False))


class FarmExtraVisibleErrorTests(TestCase):
    """Wonder blocker 1: tampered/service errors must be VISIBLE (no swallowed add_error)."""

    def test_tampered_other_farm_extra_id_shows_visible_error(self):
        from .models import FarmExtraField as M
        u, farm, c = make_owner(email="t1vis@pelda.hu")
        other_u = User.objects.create_user(email="t1visother@pelda.hu", password="TesztJelszo123!")
        other_farm = Farm.objects.create(owner=other_u, name="Másik")
        victim = M.objects.create(farm=other_farm, key="Titok", value="eredeti")
        r = c.post("/gazdasagom/", extras_post(
            [{"id": victim.pk, "key": "Titok", "value": "feltört"}], initial=0))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Érvénytelen mezőazonosító")


class FarmExtraServiceBoundaryTests(TestCase):
    """Wonder blocker 2: service-level 20/64/500 + nonblank + casefold invariants."""

    def _ns_forms(self, farm, pairs):
        from types import SimpleNamespace
        from .models import FarmExtraField as M
        forms = []
        for k, v in pairs:
            forms.append(SimpleNamespace(
                instance=M(farm=farm, key=k, value=v),
                cleaned_data={"key": k, "value": v, "DELETE": False},
            ))
        return SimpleNamespace(forms=forms)

    def test_direct_21_rows_rejected_with_rollback(self):
        from django.core.exceptions import ValidationError
        from .services import save_farm_profile_with_extras
        from .models import FarmExtraField as M
        u, farm, c = make_owner(email="t1svc21@pelda.hu")
        pairs = [(f"mező {i}", "x") for i in range(21)]
        with self.assertRaises(ValidationError):
            save_farm_profile_with_extras(farm, {"name": farm.name}, self._ns_forms(farm, pairs))
        farm.refresh_from_db()
        self.assertEqual(farm.profile_version, 1)
        self.assertEqual(M.objects.filter(farm=farm).count(), 0)

    def test_direct_blank_unicode_duplicate_length_rollback(self):
        from django.core.exceptions import ValidationError
        from .services import save_farm_profile_with_extras, create_farm_with_extras
        from .models import FarmExtraField as M
        u, farm, c = make_owner(email="t1svcb@pelda.hu")
        # blank label with value
        with self.assertRaises(ValidationError):
            save_farm_profile_with_extras(
                farm, {"name": farm.name}, self._ns_forms(farm, [("  ", "van érték")]))
        # unicode casefold duplicate
        with self.assertRaises(ValidationError):
            save_farm_profile_with_extras(
                farm, {"name": farm.name}, self._ns_forms(farm, [("Öntözés", "a"), ("  öntözés ", "b")]))
        # overlong key / value
        with self.assertRaises(ValidationError):
            save_farm_profile_with_extras(
                farm, {"name": farm.name}, self._ns_forms(farm, [("k" * 65, "ok")]))
        with self.assertRaises(ValidationError):
            save_farm_profile_with_extras(
                farm, {"name": farm.name}, self._ns_forms(farm, [("ok", "v" * 501)]))
        # create-path 21-row + duplicate rollback (no farm left behind)
        n_farms = Farm.objects.count()
        with self.assertRaises(ValidationError):
            create_farm_with_extras(
                User.objects.create_user(email="t1svcc@pelda.hu", password="TesztJelszo123!"),
                {"name": "Új"}, [(f"m{i}", "x") for i in range(21)])
        with self.assertRaises(ValidationError):
            create_farm_with_extras(
                User.objects.create_user(email="t1svcd@pelda.hu", password="TesztJelszo123!"),
                {"name": "Új2"}, [("Öntözés", "a"), ("ÖNTÖZÉS", "b")])
        self.assertEqual(Farm.objects.count(), n_farms)
        farm.refresh_from_db()
        self.assertEqual(farm.profile_version, 1)
        self.assertEqual(M.objects.filter(farm=farm).count(), 0)
