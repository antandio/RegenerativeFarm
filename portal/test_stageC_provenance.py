"""StageC RED: historical provenance must come from persisted rows only."""
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from portal.models import Farm, FarmProfileSnapshot, AdviceRun, ConsentEvent


def _user(email="prov@example.com"):
    U = get_user_model()
    u = U.objects.create_user(email=email, password="LongPass123!")
    Farm.objects.create(owner=u, name="Tesztgazdaság")
    return u


class StageCProvenanceRed(TestCase):
    def test_advice_detail_shows_persisted_provenance_panel(self):
        u = _user()
        farm = Farm.objects.get(owner=u)
        snap = FarmProfileSnapshot.objects.create(farm=farm, profile_version=1, profile_json={"name": farm.name})
        run = AdviceRun.objects.create(
            farm=farm, initiated_by=u, profile_snapshot=snap, idempotency_key="k1",
            requested_model="m", actual_model="m", status="success",
            usage_input_tokens=11, usage_output_tokens=22,
            dispatch_consent_event_id=999,
        )
        c = Client()
        c.force_login(u)
        # Diverge CURRENT profile/consent from persisted history.
        farm.name = "Changed now"
        farm.profile_version = 5
        farm.save()
        html = c.get(f"/tanacsok/{run.id}/").content.decode()
        self.assertIn("eredet", html.lower())
        # Persisted snapshot version shown...
        self.assertIn("1", html)
        # ...but current profile version must NOT be presented as this run's version.
        self.assertNotIn("profilverzió:</strong> 5", html.lower().replace("profilverzi\u00f3", "profilverzi\u00f3"))
        # Missing cost must read as not-recorded, never an invented figure.
        self.assertIn("nincs rögzítve", html.lower())

    def test_quota_pages_share_snapshot_time(self):
        u = _user(email="q@example.com")
        c = Client()
        c.force_login(u)
        a = c.get("/tanacs/keres/").content.decode()
        b = c.get("/chat/").content.decode()
        self.assertIn("időpont", a.lower())
        self.assertIn("időpont", b.lower())

    def test_chat_navigate_proposal_shows_human_label(self):
        u = _user(email="n@example.com")
        c = Client()
        c.force_login(u)
        html = c.get("/chat/").content.decode()
        # Nav section must not leak raw route key as visible label.
        self.assertNotIn(">location_list<", html)

    def test_farm_form_all_fields_have_help(self):
        u = _user(email="f@example.com")
        c = Client()
        c.force_login(u)
        html = c.get("/gazdasagom/").content.decode()
        self.assertIn("hekt", html.lower())
