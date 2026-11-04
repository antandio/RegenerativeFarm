from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from datetime import timedelta
import io
from .models import Farm, FarmProfileSnapshot, AdviceRun, AdviceStep, FarmAction, ConsentEvent, TermsAcceptance, current_consent_granted
from .services import request_advice, AdviceRejected
from .provider import ProviderTimeout, ProviderInvalidOutput, ProviderError

User = get_user_model()

def make_user(email="anna@pelda.hu", pw="TesztJelszo123!"):
    return User.objects.create_user(email=email, password=pw)

def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")

def good_payload():
    return {"steps":[
        {"title":f"Lépés {i}","action_text":f"Heti megfigyelés {i} rögzítése a profil alapján.","rationale":"A profilban szereplő talaj- és vízjegyzetekre épül.","assumptions":["A megfigyelés rendszeres"],"risk_note":"Alacsony kockázat, visszafordítható.","expert_referral":None} for i in (1,2,3)],
        "assumptions":["A profil adatai aktuálisak"],"focused_questions":["Mekkora a pontos parcella mérete?"],"is_general":False,
        "_actual_model":"muse-spark-1.3-test","_usage_input_tokens":100,"_usage_output_tokens":200}

class FakeClient:
    def __init__(self, payload=None, exc=None):
        self.payload = payload; self.exc = exc; self.calls = 0
    def generate_advice(self, profile):
        self.calls += 1
        if self.exc: raise self.exc
        return self.payload

class AuthTests(TestCase):
    def test_register_creates_user_terms_and_one_farm(self):
        r = self.client.post("/regisztracio/", {"email":"Uj@Pelda.HU","password":"TesztJelszo123!","password2":"TesztJelszo123!","accept_terms":"on"})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(email="uj@pelda.hu")
        self.assertTrue(u.check_password("TesztJelszo123!"))
        self.assertTrue(TermsAcceptance.objects.filter(user=u).exists())
        self.assertEqual(Farm.objects.filter(owner=u).count(), 1)
    def test_register_duplicate_email_case_insensitive_rejected(self):
        make_user("anna@pelda.hu")
        r = self.client.post("/regisztracio/", {"email":"ANNA@pelda.hu","password":"TesztJelszo123!","password2":"TesztJelszo123!","accept_terms":"on"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(User.objects.filter(email__iexact="anna@pelda.hu").count(), 1)
    def test_login_logout_and_unauth_redirect(self):
        make_user()
        r = self.client.get("/attekintes/")
        self.assertEqual(r.status_code, 302)
        r = self.client.post("/belepes/", {"email":"anna@pelda.hu","password":"TesztJelszo123!"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/attekintes/").status_code, 200)
        self.client.post("/kilepes/")
        self.assertEqual(self.client.get("/attekintes/").status_code, 302)
    def test_login_throttle_after_five_failures(self):
        make_user()
        for _ in range(5):
            self.client.post("/belepes/", {"email":"anna@pelda.hu","password":"rossz-jelszo-123"})
        r = self.client.post("/belepes/", {"email":"anna@pelda.hu","password":"TesztJelszo123!"})
        self.assertEqual(r.status_code, 429)
    def test_health_no_secrets(self):
        r = self.client.get("/healthz/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"META", r.content)

class FarmTests(TestCase):
    def setUp(self):
        self.u = make_user(); self.farm = Farm.objects.create(owner=self.u, name="Tesztfarm")
        self.client.force_login(self.u)
    def test_profile_edit_bumps_version_and_missing_marked(self):
        r = self.client.post("/gazdasagom/", {"name":"Tesztfarm","region_text":"Duna-völgy","climate_text":"","farming_direction_text":"","soil_notes":"Löszös","water_notes":"","crops_livestock_notes":"","equipment_labour_notes":"","goals_notes":"","constraints_notes":""})
        self.assertEqual(r.status_code, 302)
        self.farm.refresh_from_db()
        self.assertEqual(self.farm.profile_version, 2)
        self.assertIn("water_notes", self.farm.completeness_missing())
    def test_second_farm_not_created(self):
        self.client.post("/gazdasagom/", {"name":"Masodik","region_text":"","climate_text":"","farming_direction_text":"","soil_notes":"","water_notes":"","crops_livestock_notes":"","equipment_labour_notes":"","goals_notes":"","constraints_notes":""})
        self.assertEqual(Farm.objects.filter(owner=self.u).count(), 1)

class IsolationTests(TestCase):
    def setUp(self):
        self.a = make_user("a@pelda.hu"); self.b = make_user("b@pelda.hu")
        self.fa = Farm.objects.create(owner=self.a, name="A farm")
        self.fb = Farm.objects.create(owner=self.b, name="B farm")
        grant(self.fa, self.a)
        self.client.force_login(self.a)
        run = request_advice(self.a, self.fa, "iso-key-1", client=FakeClient(payload=good_payload()))
        self.run = AdviceRun.objects.get(pk=run.pk)
        self.step = self.run.steps.first()
        self.action = FarmAction.objects.create(farm=self.fa, text="A teendője", status="open")
    def test_other_user_gets_404_on_advice_and_action_edit(self):
        c = Client(); c.force_login(self.b)
        self.assertEqual(c.get(f"/tanacsok/{self.run.id}/").status_code, 404)
        self.assertEqual(c.get(f"/teendok/{self.action.id}/szerkesztes/").status_code, 404)
        r = c.post(f"/teendok/{self.action.id}/allapot/")
        self.assertEqual(r.status_code, 404)
        self.action.refresh_from_db(); self.assertEqual(self.action.status, "open")
    def test_action_from_other_farm_step_blocked(self):
        c = Client(); c.force_login(self.b)
        # B's own run id does not exist with A's step; using A's run id must 404
        r = c.post(f"/tanacsok/{self.run.id}/lepes/{self.step.id}/akcio/")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(FarmAction.objects.filter(farm=self.fb, source_step=self.step).exists())
    def test_export_contains_only_own_data(self):
        r = self.client.get("/fiok/export/")
        body = r.content.decode()
        self.assertIn("A teendője", body)
        self.assertNotIn("b@pelda.hu", body)

class AdviceServiceTests(TestCase):
    def setUp(self):
        self.u = make_user(); self.farm = Farm.objects.create(owner=self.u, name="Tanácsfarm", soil_notes="Homokos talaj", water_notes="Kút van")
    def test_no_consent_no_http_call(self):
        fake = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected):
            request_advice(self.u, self.farm, "k-noconsent", client=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(AdviceRun.objects.count(), 0)
    def test_terms_alone_does_not_grant(self):
        TermsAcceptance.objects.create(user=self.u, terms_version="aszf_v1", privacy_notice_version="tajekoztato_v1")
        fake = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected):
            request_advice(self.u, self.farm, "k-terms", client=fake)
        self.assertEqual(fake.calls, 0)
    def test_success_exactly_three_steps_and_snapshot(self):
        grant(self.farm, self.u)
        run = request_advice(self.u, self.farm, "k-ok", client=FakeClient(payload=good_payload()))
        self.assertEqual(run.status, "success")
        self.assertEqual(run.steps.count(), 3)
        self.assertEqual(run.profile_snapshot.profile_version, 1)
        self.assertIsNotNone(run.result_assumptions)
    def test_two_steps_fails_without_steps(self):
        grant(self.farm, self.u)
        p = good_payload(); p["steps"] = p["steps"][:2]
        run = request_advice(self.u, self.farm, "k-2steps", client=FakeClient(payload=p))
        self.assertEqual(run.status, "failed"); self.assertEqual(run.error_code, "invalid_structured_output"); self.assertEqual(AdviceStep.objects.count(), 0)
    def test_four_steps_fails(self):
        grant(self.farm, self.u)
        p = good_payload(); p["steps"] = p["steps"] + [dict(p["steps"][0])]
        run = request_advice(self.u, self.farm, "k-4steps", client=FakeClient(payload=p))
        self.assertEqual(run.status, "failed"); self.assertEqual(AdviceStep.objects.count(), 0)
    def test_prohibited_content_blocked(self):
        grant(self.farm, self.u)
        p = good_payload(); p["steps"][0]["action_text"] = "Alkalmazzon műtrágya adag: 200 kg/ha mennyiséget."
        run = request_advice(self.u, self.farm, "k-prohib", client=FakeClient(payload=p))
        self.assertEqual(run.status, "failed"); self.assertEqual(run.error_code, "prohibited_content"); self.assertEqual(AdviceStep.objects.count(), 0)
    def test_timeout_sanitized(self):
        grant(self.farm, self.u)
        run = request_advice(self.u, self.farm, "k-timeout", client=FakeClient(exc=ProviderTimeout("x")))
        self.assertEqual(run.status, "failed"); self.assertEqual(run.error_code, "provider_timeout")
        self.assertNotIn("META", run.error_message_safe or "")
    def test_idempotency_single_run_single_call(self):
        grant(self.farm, self.u)
        fake = FakeClient(payload=good_payload())
        r1 = request_advice(self.u, self.farm, "k-dup", client=fake)
        r2 = request_advice(self.u, self.farm, "k-dup", client=fake)
        self.assertEqual(r1.pk, r2.pk); self.assertEqual(fake.calls, 1); self.assertEqual(AdviceRun.objects.count(), 1)
    def test_quota_counts_pending_and_failed(self):
        grant(self.farm, self.u)
        snap, _ = FarmProfileSnapshot.objects.get_or_create(farm=self.farm, profile_version=1, defaults={"profile_json": self.farm.profile_dict()})
        for i in range(5):
            AdviceRun.objects.create(farm=self.farm, initiated_by=self.u, profile_snapshot=snap, idempotency_key=f"old-{i}", status="failed", error_code="provider_error", error_message_safe="hiba", completed_at=timezone.now())
        fake = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected) as ctx:
            request_advice(self.u, self.farm, "k-quota", client=fake)
        self.assertEqual(ctx.exception.code, "quota_exceeded"); self.assertEqual(fake.calls, 0)
    def test_stale_pending_cleaned_on_next_request(self):
        grant(self.farm, self.u)
        snap, _ = FarmProfileSnapshot.objects.get_or_create(farm=self.farm, profile_version=1, defaults={"profile_json": self.farm.profile_dict()})
        stale = AdviceRun.objects.create(farm=self.farm, initiated_by=self.u, profile_snapshot=snap, idempotency_key="stale-1", status="pending", attempt_started_at=timezone.now()-timedelta(minutes=60), dispatch_authorized_at=timezone.now()-timedelta(minutes=60))
        # hour quota is 5; stale + 0 others is fine, request proceeds and stale becomes failed
        run = request_advice(self.u, self.farm, "k-after-stale", client=FakeClient(payload=good_payload()))
        stale.refresh_from_db()
        self.assertEqual(stale.status, "failed"); self.assertEqual(stale.error_code, "attempt_interrupted")
        self.assertEqual(run.status, "success")
    def test_snapshot_immutable_and_history_preserved(self):
        grant(self.farm, self.u)
        run = request_advice(self.u, self.farm, "k-snap", client=FakeClient(payload=good_payload()))
        snap = run.profile_snapshot
        self.client.force_login(self.u)
        self.client.post("/gazdasagom/", {"name":"Tanácsfarm","region_text":"Új régió","climate_text":"","farming_direction_text":"","soil_notes":"Más talaj","water_notes":"","crops_livestock_notes":"","equipment_labour_notes":"","goals_notes":"","constraints_notes":""})
        snap.refresh_from_db()
        self.assertEqual(snap.profile_json["soil_notes"], "Homokos talaj")
        run2 = request_advice(self.u, Farm.objects.get(pk=self.farm.pk), "k-snap2", client=FakeClient(payload=good_payload()))
        self.assertEqual(run2.profile_snapshot.profile_version, 2)
        with self.assertRaises(ValueError):
            snap.profile_json = {}; snap.save()
    def test_revoked_blocks_but_history_readable(self):
        grant(self.farm, self.u)
        run = request_advice(self.u, self.farm, "k-rev", client=FakeClient(payload=good_payload()))
        ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="revoked")
        fake = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected):
            request_advice(self.u, self.farm, "k-rev2", client=fake)
        self.assertEqual(fake.calls, 0)
        self.client.force_login(self.u)
        self.assertEqual(self.client.get(f"/tanacsok/{run.id}/").status_code, 200)

class ActionTests(TestCase):
    def setUp(self):
        self.u = make_user(); self.farm = Farm.objects.create(owner=self.u, name="Akciófarm")
        grant(self.farm, self.u)
        self.client.force_login(self.u)
        run = request_advice(self.u, self.farm, "act-key", client=FakeClient(payload=good_payload()))
        self.step = run.steps.first()
    def test_free_action_toggle_edit_delete(self):
        self.client.post("/teendok/uj/", {"text":"Szabad teendő"})
        a = FarmAction.objects.get(farm=self.farm, text="Szabad teendő")
        self.assertIsNone(a.source_step)
        self.client.post(f"/teendok/{a.id}/allapot/")
        a.refresh_from_db(); self.assertEqual(a.status, "done"); self.assertIsNotNone(a.done_at)
        self.client.post(f"/teendok/{a.id}/allapot/")
        a.refresh_from_db(); self.assertEqual(a.status, "open"); self.assertIsNone(a.done_at)
        self.client.post(f"/teendok/{a.id}/szerkesztes/", {"text":"Módosított teendő"})
        a.refresh_from_db(); self.assertEqual(a.text, "Módosított teendő")
        self.client.post(f"/teendok/{a.id}/torles/")
        self.assertFalse(FarmAction.objects.filter(pk=a.id).exists())
    def test_action_from_step_copies_text(self):
        r = self.client.post(f"/tanacsok/{self.step.advice_run.id}/lepes/{self.step.id}/akcio/")
        self.assertEqual(r.status_code, 302)
        a = FarmAction.objects.get(farm=self.farm, source_step=self.step)
        self.assertEqual(a.text, self.step.action_text)

class AccountTests(TestCase):
    def setUp(self):
        self.u = make_user(); self.farm = Farm.objects.create(owner=self.u, name="Fiókfarm")
        self.client.force_login(self.u)
    def test_consent_grant_revoke_independent(self):
        self.assertFalse(current_consent_granted(self.farm))
        self.client.post("/fiok/hozzajarulas/", {"consent_action":"grant"})
        self.assertTrue(current_consent_granted(self.farm))
        self.client.post("/fiok/hozzajarulas/", {"consent_action":"revoke"})
        self.assertFalse(current_consent_granted(self.farm))
        self.assertEqual(ConsentEvent.objects.filter(farm=self.farm).count(), 2)
    def test_account_delete_removes_data_and_invalidates_session(self):
        FarmAction.objects.create(farm=self.farm, text="Törlendő teendő")
        r = self.client.post("/fiok/torles/", {"confirm_text":"TORLES"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.u.pk).exists())
        self.assertFalse(Farm.objects.filter(pk=self.farm.pk).exists())
        self.assertEqual(self.client.get("/attekintes/").status_code, 302)
    def test_delete_requires_exact_confirmation(self):
        r = self.client.post("/fiok/torles/", {"confirm_text":"torles"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.u.pk).exists())

class SeedTests(TestCase):
    def test_seed_idempotent_synthetic_no_live_call(self):
        out = io.StringIO()
        with patch("portal.services.MetaProviderClient.generate_advice") as mock_gen:
            call_command("seed_demo", stdout=out)
            call_command("seed_demo", stdout=out)
            self.assertEqual(mock_gen.call_count, 0)
        self.assertEqual(Farm.objects.count(), 3)
        runs = AdviceRun.objects.all()
        self.assertTrue(runs.exists())
        self.assertTrue(all(r.source == "seed" for r in runs))
        # rerun did not duplicate farms
        self.assertEqual(User.objects.filter(email__endswith="@demo.pelda.hu").count(), 3)

import httpx as _httpx
from django.core.exceptions import ValidationError as _VE
from .provider import MetaProviderClient as _MPC, build_messages as _bm

class ExtraEdgeTests(TestCase):
    def setUp(self):
        self.u = make_user("edge@pelda.hu"); self.farm = Farm.objects.create(owner=self.u, name="Peremfarm", soil_notes="Homok")
        grant(self.farm, self.u)
    def test_cross_farm_action_rejected_at_model_layer(self):
        other = make_user("masik@pelda.hu"); ofarm = Farm.objects.create(owner=other, name="Másik farm")
        grant(ofarm, other)
        run = request_advice(other, ofarm, "edge-x", client=FakeClient(payload=good_payload()))
        step = run.steps.first()
        with self.assertRaises(_VE):
            FarmAction.objects.create(farm=self.farm, source_step=step, source_step_farm_id=self.farm.id, text="tiltott hivatkozás")
    def test_step_position_four_rejected_by_db(self):
        run = request_advice(self.u, self.farm, "edge-pos", client=FakeClient(payload=good_payload()))
        from django.db import IntegrityError
        with self.assertRaises(Exception):
            AdviceStep.objects.create(advice_run=run, farm=self.farm, position=4, title="x", action_text="y", rationale="z", risk_note="r")
    def test_provider_http_success_via_mock_transport(self):
        import json as _json
        def handler(request):
            return _httpx.Response(200, json={"model":"muse-spark-1.3","usage":{"prompt_tokens":10,"completion_tokens":20},"choices":[{"message":{"content": _json.dumps({"steps":good_payload()["steps"],"assumptions":[],"focused_questions":[],"is_general":False})}}]})
        client = _MPC(api_key="test-key-not-real", transport=_httpx.MockTransport(handler))
        out = client.generate_advice({"name":"Teszt"})
        self.assertEqual(len(out["steps"]), 3)
    def test_provider_timeout_and_http500_sanitized(self):
        def slow(request):
            raise _httpx.ReadTimeout("slow", request=request)
        c1 = _MPC(api_key="k", transport=_httpx.MockTransport(slow))
        with self.assertRaises(ProviderTimeout):
            c1.generate_advice({"name":"T"})
        def err(request):
            return _httpx.Response(500, text="internal secret detail")
        c2 = _MPC(api_key="k", transport=_httpx.MockTransport(err))
        with self.assertRaises(ProviderError) as ctx:
            c2.generate_advice({"name":"T"})
        self.assertNotIn("secret", str(ctx.exception))
    def test_bounded_input_payload(self):
        # Task1: the fixed trusted policy now travels in the system message;
        # the untrusted farm-profile payload itself stays bounded.
        msgs = _bm({"name":"Teszt","soil_notes":"x"*10000})
        user_msg = [m for m in msgs if m["role"] == "user"][0]["content"]
        self.assertLess(len(user_msg), 12000)
        self.assertIn("Regenerative Agriculture System Architect Skill",
                      msgs[0]["content"])
    def test_csrf_required_on_post(self):
        from django.test import Client as _C
        c = _C(enforce_csrf_checks=True); c.force_login(self.u)
        r = c.post("/teendok/uj/", {"text":"csrf teszt"})
        self.assertEqual(r.status_code, 403)
    def test_invalid_json_response_fails_safely(self):
        class BadClient:
            def generate_advice(self, profile):
                raise ProviderInvalidOutput("bad json")
        run = request_advice(self.u, self.farm, "edge-badjson", client=BadClient())
        self.assertEqual(run.status, "failed"); self.assertEqual(run.error_code, "invalid_structured_output")

class DeployedStaticTests(TestCase):
    """Regression: deployed WSGI (gunicorn, DEBUG=False) must serve portal CSS.

    Django does not serve static files in production; without WhiteNoise
    middleware + STATIC_ROOT/collectstatic, /static/portal/style.css is 404.
    """
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings as _s
        if getattr(_s, "STATIC_ROOT", None):
            call_command("collectstatic", interactive=False, verbosity=0)

    @override_settings(DEBUG=False)
    def test_portal_css_served_via_wsgi_when_debug_false(self):
        r = self.client.get("/static/portal/style.css")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("text/css"), r["Content-Type"])
        body = b"".join(r.streaming_content) if getattr(r, "streaming", False) else r.content
        self.assertIn(b"--forest", body)


# ---------------------------------------------------------------------------
# Security-review regression tests (written before the fixes; see TDD_LOG).
# Each class maps to a blocking review item. Stale-instance simulations are
# used where true multi-connection file-backed concurrency is not available
# in the Django test DB; that limitation is documented in README/TDD_LOG.
# ---------------------------------------------------------------------------

class Blocking1StaleFarmTests(TestCase):
    def setUp(self):
        self.u = make_user("b1@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="B1 farm", soil_notes="Régi talaj", water_notes="Kút")
        grant(self.farm, self.u)

    def test_advice_reservation_reloads_farm_not_stale_instance(self):
        stale = Farm.objects.get(pk=self.farm.pk)  # version 1, old text
        # Another writer commits version 2 meanwhile (queryset update = the other tx).
        Farm.objects.filter(pk=self.farm.pk).update(soil_notes="FRISS talaj", profile_version=2)
        run = request_advice(self.u, stale, "b1-stale", client=FakeClient(payload=good_payload()))
        self.assertEqual(run.status, "success")
        self.assertEqual(run.profile_snapshot.profile_version, 2)
        self.assertEqual(run.profile_snapshot.profile_json["soil_notes"], "FRISS talaj")

    def test_profile_update_helper_uses_fresh_version(self):
        from .services import update_farm_profile
        stale = Farm.objects.get(pk=self.farm.pk)
        Farm.objects.filter(pk=self.farm.pk).update(soil_notes="Közben mentett", profile_version=2)
        data = {"name": "B1 farm", "region_text": None, "climate_text": None,
                "farming_direction_text": None, "soil_notes": "Új mentés", "water_notes": "Kút",
                "crops_livestock_notes": None, "equipment_labour_notes": None,
                "goals_notes": None, "constraints_notes": None}
        fresh = update_farm_profile(stale, data)
        self.assertEqual(fresh.profile_version, 3)  # not duplicate 2
        fresh.refresh_from_db()
        self.assertEqual(fresh.profile_version, 3)
        self.assertEqual(fresh.soil_notes, "Új mentés")

    def test_advice_reservation_rejects_non_owner(self):
        other = make_user("b1-other@pelda.hu")
        fake = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected):
            request_advice(other, self.farm, "b1-owner", client=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(AdviceRun.objects.count(), 0)


class Blocking2ConsentDispatchTests(TestCase):
    def setUp(self):
        self.u = make_user("b2@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="B2 farm", soil_notes="Homok", water_notes="Kút")
        grant(self.farm, self.u)

    def test_pause_after_reservation_revoke_then_dispatch_zero_calls(self):
        from .services import reserve_advice_run, execute_reserved_run
        run = reserve_advice_run(self.u, self.farm, "b2-pause")
        self.assertEqual(run.status, "pending")
        self.assertIsNone(run.dispatch_authorized_at)  # reservation is NOT authorization
        ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="revoked")
        fake = FakeClient(payload=good_payload())
        result = execute_reserved_run(run, client=fake)
        self.assertEqual(fake.calls, 0)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "consent_withdrawn_before_call")
        self.assertIsNone(result.dispatch_authorized_at)
        self.assertEqual(AdviceStep.objects.count(), 0)

    def test_dispatch_authorization_recorded_on_success(self):
        run = request_advice(self.u, self.farm, "b2-ok", client=FakeClient(payload=good_payload()))
        self.assertEqual(run.status, "success")
        self.assertIsNotNone(run.dispatch_authorized_at)

    def test_revoke_during_inflight_call_completes_then_future_blocked(self):
        farm, user = self.farm, self.u

        class RevokingClient:
            def __init__(self):
                self.calls = 0

            def generate_advice(self, profile):
                self.calls += 1
                ConsentEvent.objects.create(farm=farm, actor=user, event_type="revoked")
                return good_payload()

        run = request_advice(self.u, self.farm, "b2-inflight", client=RevokingClient())
        # Dispatch was authorized before the call, so the in-flight call
        # (already sent, cannot be unsent) completes; revocation is future-only.
        self.assertEqual(run.status, "success")
        self.assertEqual(run.steps.count(), 3)
        fake2 = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected):
            request_advice(self.u, self.farm, "b2-future", client=fake2)
        self.assertEqual(fake2.calls, 0)


class Blocking3MetadataAndSafetyTests(TestCase):
    def setUp(self):
        self.u = make_user("b3@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="B3 farm", soil_notes="Homok", water_notes="Kút")
        grant(self.farm, self.u)

    def _run_with(self, mutate, key):
        p = good_payload()
        mutate(p)
        return request_advice(self.u, self.farm, key, client=FakeClient(payload=p))

    def test_boolean_usage_tokens_rejected_terminal(self):
        run = self._run_with(lambda p: p.update({"_usage_input_tokens": True}), "b3-bool")
        self.assertEqual(run.status, "failed")
        self.assertEqual(AdviceStep.objects.count(), 0)

    def test_oversized_usage_tokens_rejected_terminal(self):
        run = self._run_with(lambda p: p.update({"_usage_output_tokens": 2 ** 100}), "b3-big")
        self.assertEqual(run.status, "failed")
        self.assertEqual(AdviceStep.objects.count(), 0)

    def test_malformed_actual_model_rejected_terminal(self):
        for i, bad in enumerate((123, "x" * 500, ["m"], {"m": 1})):
            run = self._run_with(lambda p, b=bad: p.update({"_actual_model": b}), f"b3-model-{i}")
            self.assertEqual(run.status, "failed", bad)
        self.assertEqual(AdviceStep.objects.count(), 0)
        self.assertFalse(AdviceRun.objects.filter(status="pending").exists())

    def test_provider_boolean_usage_rejected_before_return(self):
        import json as _json
        import httpx as _h
        from .provider import MetaProviderClient as _C

        def handler(request):
            return _h.Response(200, json={"model": "m", "usage": {"prompt_tokens": True, "completion_tokens": 5},
                                          "choices": [{"message": {"content": _json.dumps({"steps": good_payload()["steps"], "assumptions": [], "focused_questions": [], "is_general": False})}}]})

        c = _C(api_key="k", transport=_h.MockTransport(handler))
        with self.assertRaises(ProviderInvalidOutput):
            c.generate_advice({"name": "T"})

    def test_finalization_error_is_sanitized_terminal_no_partial(self):
        from unittest.mock import patch as _p
        with _p.object(AdviceStep.objects, "create", side_effect=RuntimeError("titkos-db-reszlet")):
            run = request_advice(self.u, self.farm, "b3-fin", client=FakeClient(payload=good_payload()))
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertNotIn("titkos", run.error_message_safe or "")
        self.assertEqual(AdviceStep.objects.count(), 0)

    def test_safety_screen_examines_focused_questions(self):
        def mut(p):
            p["focused_questions"] = ["Alkalmazzon 200 kg/ha mennyiséget?"]

        run = self._run_with(mut, "b3-fq")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "prohibited_content")
        self.assertEqual(AdviceStep.objects.count(), 0)


class Blocking5StaleKeyAndCleanupTests(TestCase):
    def setUp(self):
        self.u = make_user("b5@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="B5 farm", soil_notes="Homok", water_notes="Kút")
        grant(self.farm, self.u)
        self.snap, _ = FarmProfileSnapshot.objects.get_or_create(
            farm=self.farm, profile_version=1, defaults={"profile_json": self.farm.profile_dict()})

    def _stale(self, key):
        # Authorized interrupted dispatch: claimed (dispatch_authorized_at)
        # and past the safe deadline, so the durable reaper may fail it.
        return AdviceRun.objects.create(
            farm=self.farm, initiated_by=self.u, profile_snapshot=self.snap,
            idempotency_key=key, status="pending",
            attempt_started_at=timezone.now() - timedelta(minutes=60),
            dispatch_authorized_at=timezone.now() - timedelta(minutes=60))

    def _unclaimed(self, key):
        return AdviceRun.objects.create(
            farm=self.farm, initiated_by=self.u, profile_snapshot=self.snap,
            idempotency_key=key, status="pending",
            attempt_started_at=timezone.now() - timedelta(minutes=60),
            dispatch_authorized_at=None)

    def test_same_stale_key_transitions_failed_no_provider_call(self):
        stale = self._stale("b5-same")
        fake = FakeClient(payload=good_payload())
        run = request_advice(self.u, self.farm, "b5-same", client=fake)
        self.assertEqual(fake.calls, 0)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "attempt_interrupted")
        self.assertEqual(run.pk, stale.pk)

    def test_cleanup_persists_even_when_other_reservation_rejected(self):
        stale = self._stale("b5-other-stale")
        ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="revoked")
        fake = FakeClient(payload=good_payload())
        with self.assertRaises(AdviceRejected):
            request_advice(self.u, self.farm, "b5-new-rejected", client=fake)
        stale.refresh_from_db()
        self.assertEqual(stale.status, "failed")
        self.assertEqual(stale.error_code, "attempt_interrupted")
        self.assertEqual(fake.calls, 0)

    def test_queued_unclaimed_never_reaped(self):
        # Unclaimed (never dispatch-authorized) queued runs are durable:
        # even when old, the reaper/next request must keep them pending.
        queued = self._unclaimed("b5-unclaimed")
        fake = FakeClient(payload=good_payload())
        run = request_advice(self.u, self.farm, "b5-distinct-while-queued", client=fake)
        queued.refresh_from_db()
        self.assertEqual(queued.status, "pending")
        self.assertIsNone(queued.dispatch_authorized_at)
        self.assertEqual(run.pk, queued.pk)
        self.assertEqual(fake.calls, 0)

    def test_reserve_rejects_non_owner(self):
        from .services import reserve_advice_run
        other = make_user("b5-other@pelda.hu")
        with self.assertRaises(AdviceRejected):
            reserve_advice_run(other, self.farm, "b5-res-owner")
        self.assertEqual(AdviceRun.objects.count(), 0)


class LoginThrottleDbTests(TestCase):
    def test_throttle_survives_process_cache_clear(self):
        from django.core.cache import cache as _cache
        make_user("thr@pelda.hu")
        for _ in range(5):
            self.client.post("/belepes/", {"email": "thr@pelda.hu", "password": "rossz-jelszo-123"})
        _cache.clear()  # simulates the other gunicorn worker's empty cache
        r = self.client.post("/belepes/", {"email": "thr@pelda.hu", "password": "TesztJelszo123!"})
        self.assertEqual(r.status_code, 429)

    def test_throttle_expired_window_allows_login_and_cleans_up(self):
        from .models import LoginThrottle
        make_user("thr2@pelda.hu")
        for _ in range(5):
            self.client.post("/belepes/", {"email": "thr2@pelda.hu", "password": "rossz-jelszo-123"})
        self.assertTrue(LoginThrottle.objects.exists())
        LoginThrottle.objects.all().update(expires_at=timezone.now() - timedelta(minutes=1))
        r = self.client.post("/belepes/", {"email": "thr2@pelda.hu", "password": "TesztJelszo123!"})
        self.assertEqual(r.status_code, 302)

    def test_throttle_counts_stored_in_shared_db(self):
        from .login_throttle import record_login_failure, is_login_throttled
        for _ in range(5):
            record_login_failure("10.0.0.9", "valaki@pelda.hu")
        self.assertTrue(is_login_throttled("10.0.0.9", "valaki@pelda.hu"))


class AdminDisabledTests(TestCase):
    def test_admin_route_disabled(self):
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)
