"""StageC wiring RED: dashboard/history/chat provenance + UX13/15/16/18/20/22/25/26/27."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from portal.models import Farm, FarmProfileSnapshot, AdviceRun, ConsentEvent

PW = "LongPass123!"


def _user(email="wiring@example.com"):
    U = get_user_model()
    u = U.objects.create_user(email=email, password=PW)
    farm = Farm.objects.create(owner=u, name="Wiring Farm")
    ConsentEvent.objects.create(farm=farm, actor=u, event_type="granted", policy_version="cloud_disclosure_v1")
    return u, farm


def _advice(u, farm, key="wk1"):
    snap = FarmProfileSnapshot.objects.create(farm=farm, profile_version=1, profile_json={"name": farm.name})
    return AdviceRun.objects.create(
        farm=farm, initiated_by=u, profile_snapshot=snap, idempotency_key=key,
        requested_model="m", actual_model="m", status="success",
        usage_input_tokens=7, usage_output_tokens=9,
        dispatch_consent_event_id=123,
    )


class WiringProvenancePanels(TestCase):
    def test_dashboard_shows_historical_provenance(self):
        u, farm = _user()
        _advice(u, farm)
        c = Client()
        c.force_login(u)
        html = c.get(reverse("dashboard")).content.decode()
        self.assertIn("történeti", html.lower())

    def test_history_lists_per_run_provenance(self):
        u, farm = _user()
        _advice(u, farm)
        c = Client()
        c.force_login(u)
        html = c.get(reverse("advice_history")).content.decode()
        self.assertIn("történeti", html.lower())

    def test_chat_attempt_shows_provenance(self):
        from portal.chat_models import ChatAttempt
        u, farm = _user()
        ChatAttempt.objects.create(
            farm=farm, initiated_by=u, idempotency_key="ck1", language="hu",
            status="success", source="live", requested_model="m",
            user_message="kerdes", answer_text="valasz",
            usage_input_tokens=3, usage_output_tokens=4)
        c = Client()
        c.force_login(u)
        html = c.get(reverse("chat")).content.decode()
        self.assertIn("történeti", html.lower())


class UX13DashboardCompact(TestCase):
    def test_dashboard_next_task_weather_advice_excerpt(self):
        u, farm = _user()
        run = _advice(u, farm)
        from portal.models import AdviceStep
        AdviceStep.objects.create(
            advice_run=run, farm=farm, position=1, title="Olajos leces",
            action_text="Tegyen valamit", rationale="miert", risk_note="kockazat")
        from portal.models import FarmAction
        FarmAction.objects.create(farm=farm, text="Kovetkezo locsolas", status="open")
        c = Client()
        c.force_login(u)
        html = c.get(reverse("dashboard")).content.decode().lower()
        self.assertIn("köv", html)  # Kovetkezo teendo label
        self.assertIn("időj", html)  # weather status block
        self.assertIn("olajos leces", html)  # advice excerpt visible without opening


class UX20ProposalPreview(TestCase):
    def _seed(self):
        from portal.chat_models import ChatAttempt, ChatProposal
        from portal.models import FarmAction
        u, farm = _user(email="ux20@example.com")
        att = ChatAttempt.objects.create(
            farm=farm, initiated_by=u, idempotency_key="ck20", language="hu",
            status="success", source="live", requested_model="m",
            user_message="kerdes", answer_text="valasz")
        act = FarmAction.objects.create(farm=farm, text="Celtabla", status="open")
        ChatProposal.objects.create(attempt=att, farm=farm, position=1,
                                    kind="action_create", payload_text="Valodi teendo szoveg")
        ChatProposal.objects.create(attempt=att, farm=farm, position=2,
                                    kind="action_toggle", payload_text=str(act.id))
        ChatProposal.objects.create(attempt=att, farm=farm, position=3,
                                    kind="plans_generate")
        ChatProposal.objects.create(attempt=att, farm=farm, position=4,
                                    kind="navigate", payload_link="location_list")
        return u, farm

    def test_preview_texts_human_nav_no_raw_key(self):
        u, farm = self._seed()
        c = Client()
        c.force_login(u)
        html = c.get(reverse("chat")).content.decode()
        self.assertIn("Valodi teendo szoveg", html)
        self.assertIn("Celtabla", html)
        self.assertIn("Tervk", html)  # plans_generate human effect
        self.assertNotIn("location_list", html)  # no raw route key anywhere
        self.assertIn("Helysz", html)  # human nav label

    def test_confirm_post_unchanged(self):
        from portal.chat_models import ChatProposal
        u, farm = self._seed()
        c = Client()
        c.force_login(u)
        p = ChatProposal.objects.filter(kind="action_create").first()
        r = c.get(reverse("chat_proposal_confirm", args=[p.id]))
        self.assertEqual(r.status_code, 405)


class UX22Escaping(TestCase):
    def test_answer_html_escaped_and_proportional_css(self):
        from portal.chat_models import ChatAttempt
        import pathlib
        u, farm = _user(email="ux22@example.com")
        ChatAttempt.objects.create(
            farm=farm, initiated_by=u, idempotency_key="ck22", language="hu",
            status="success", source="live", requested_model="m",
            user_message="k", answer_text="<b>felkover</b>\nmasodik sor")
        c = Client()
        c.force_login(u)
        html = c.get(reverse("chat")).content.decode()
        self.assertNotIn("<b>felkover</b>", html)
        self.assertIn("&lt;b&gt;", html)
        css = (pathlib.Path(__file__).parent / "static/portal/app.css").read_text()
        self.assertNotIn("mark_safe", html)
        self.assertIn("chat-answer", css)


class UX15Autosize(TestCase):
    def test_textarea_autosize_js_and_maxlength(self):
        u, farm = _user(email="ux15@example.com")
        c = Client()
        c.force_login(u)
        html = c.get(reverse("farm")).content.decode()
        self.assertIn("scrollHeight", html)
        self.assertIn("maxlength", html.lower())


class UX26OneFarm(TestCase):
    def test_members_clarifies_one_owned_farm(self):
        u, farm = _user(email="ux26@example.com")
        c = Client()
        c.force_login(u)
        html = c.get(reverse("members")).content.decode().lower()
        self.assertIn("egy", html)
        self.assertIn("tagság", html)


class UX25Roles(TestCase):
    def test_role_matrix_visible(self):
        u, farm = _user(email="ux25@example.com")
        c = Client()
        c.force_login(u)
        html = c.get(reverse("members")).content.decode().lower()
        self.assertIn("szerepkörök", html)
        for word in ("tulajdonos", "menedzser", "munkat", "megfigyel"):
            self.assertIn(word, html)

    def test_worker_cannot_invite(self):
        from portal.models import Membership
        U = get_user_model()
        owner, farm = _user(email="ux25w@example.com")
        worker = U.objects.create_user(email="ux25worker@example.com", password=PW)
        Membership.objects.create(farm=farm, user=worker, role="worker", is_active=True)
        c = Client()
        c.force_login(worker)
        r = c.post(reverse("invite"), {"email": "x@example.com", "role": "viewer"})
        self.assertEqual(r.status_code, 403)

    def test_viewer_cannot_send_chat(self):
        from portal.models import Membership
        U = get_user_model()
        owner, farm = _user(email="ux25o@example.com")
        viewer = U.objects.create_user(email="ux25v@example.com", password=PW)
        Membership.objects.create(farm=farm, user=viewer, role="viewer", is_active=True)
        c = Client()
        c.force_login(viewer)
        r = c.post(reverse("chat"), {"message": "hello", "cost_consent": "on",
                                     "idempotency_key": "vv1"})
        self.assertEqual(r.status_code, 403)


class UX16Titles(TestCase):
    def test_each_menu_route_has_own_title(self):
        u, farm = _user(email="ux16@example.com")
        c = Client()
        c.force_login(u)
        import re
        default = "Regeneratív átállás – gazdaportál"
        for name in ("dashboard", "farm", "location_list", "property_list",
                     "equipment_list", "cycle_list", "plans", "utilization",
                     "weather", "actions", "chat", "advice_request",
                     "advice_history", "members", "account", "info"):
            html = c.get(reverse(name)).content.decode()
            m = re.search(r"<title>(.*?)</title>", html, re.S)
            self.assertIsNotNone(m, name)
            self.assertNotEqual(m.group(1).strip(), default, name)


class UX18HelpLinks(TestCase):
    def test_chat_and_advice_link_info_and_info_compares(self):
        u, farm = _user(email="ux18@example.com")
        c = Client()
        c.force_login(u)
        chat = c.get(reverse("chat")).content.decode().lower()
        adv = c.get(reverse("advice_request")).content.decode().lower()
        info = c.get(reverse("info")).content.decode().lower()
        self.assertIn("/tudnivalok", chat)
        self.assertIn("/tudnivalok", adv)
        self.assertIn("cseveg", info)
        self.assertIn("3", info)


class UX27ExportInfo(TestCase):
    def test_account_links_to_info_and_file_endpoint_kept(self):
        u, farm = _user(email="ux27@example.com")
        c = Client()
        c.force_login(u)
        acc = c.get(reverse("account")).content.decode()
        self.assertIn(reverse("export_info"), acc)
        info = c.get(reverse("export_info")).content.decode()
        self.assertIn(reverse("export"), info)
        r = c.get(reverse("export"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/json", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
