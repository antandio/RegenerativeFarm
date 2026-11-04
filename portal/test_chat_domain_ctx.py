"""RED regression: CHAT domain context + worker plans_generate permission."""
import time
from datetime import date
from django.test import TestCase
from django.core.cache import cache
from django.contrib.auth import get_user_model
from portal.models import Farm, ConsentEvent, Membership, FarmAction
from portal.domain_models import FarmLocation, FarmProperty, Equipment, CropCycle, WorkItem
from portal.chat_models import ChatAttempt, ChatProposal
from portal.chat import build_chat_context, confirm_proposal, request_chat, ChatRejected

User = get_user_model()


def make_user(email, pw="TesztJelszo123!"):
    return User.objects.create_user(email=email, password=pw)


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


def seed_weather(farm_id, loc, tag="OWN"):
    from portal.weather import _ck
    cache.set(_ck("forecast", farm_id, loc),
              {"payload": {"current": {"temperature": 20}, "daily": [],
                            "source": "Open-Meteo", "fetched_at": "2026-01-01T00:00:00+00:00",
                            "tag": tag}, "stored_at": time.time()}, 24 * 3600)
    cache.set(_ck("climate", farm_id, loc),
              {"payload": {"period": "2015-2024", "monthly": [], "tag": tag,
                            "fetched_at": "2026-01-01T00:00:00+00:00"}, "stored_at": time.time()},
              24 * 3600)


class ChatDomainContextTests(TestCase):
    def test_context_contains_own_domain_cached_weather_no_other_farm(self):
        u = make_user("ctx-o@pelda.hu")
        farm = Farm.objects.create(owner=u, name="Ctx farm")
        loc = FarmLocation.objects.create(farm=farm, name="OwnLocAlpha", latitude="46.250000",
                                          longitude="20.140000", timezone="Europe/Budapest")
        prop = FarmProperty.objects.create(farm=farm, location=loc, name="OwnPropAlpha",
                                           kind="field", area_hectares=5)
        eq = Equipment.objects.create(farm=farm, name="OwnTractorAlpha",
                                      capabilities=["tillage"], available=True)
        cyc = CropCycle.objects.create(farm=farm, property=prop, crop_name="OwnBuzaAlpha",
                                       state="current", observations="OwnObsAlpha")
        WorkItem.objects.create(farm=farm, crop_cycle=cyc, property=prop, title="OwnWorkAlpha",
                                capability="tillage", start_date=date(2026, 3, 1),
                                end_date=date(2026, 3, 5))
        seed_weather(farm.id, loc)
        other_u = make_user("ctx-x@pelda.hu")
        farm2 = Farm.objects.create(owner=other_u, name="Other farm")
        loc2 = FarmLocation.objects.create(farm=farm2, name="OtherLocZZZ", latitude="47.000000",
                                           longitude="19.000000", timezone="Europe/Budapest")
        FarmProperty.objects.create(farm=farm2, location=loc2, name="OtherPropZZZ",
                                    kind="field", area_hectares=9)
        ctx, s = build_chat_context(farm)
        import json
        json.loads(s)  # must stay valid JSON
        self.assertLessEqual(len(s), 15000)
        for needle in ("OwnPropAlpha", "OwnTractorAlpha", "OwnBuzaAlpha", "OwnWorkAlpha",
                       "OwnLocAlpha", "OwnObsAlpha"):
            self.assertIn(needle, s)
        for banned in ("OtherPropZZZ", "OtherLocZZZ"):
            self.assertNotIn(banned, s)
        self.assertNotIn("token", s.lower())
        self.assertNotIn("ctx-x@pelda.hu", s)
        self.assertIn("weather", s.lower())
        self.assertIn("forecast", s.lower())


class WorkerPlansGenerateTests(TestCase):
    def test_worker_plans_generate_confirm_denied_no_workitem_writes(self):
        owner = make_user("wp-o@pelda.hu")
        farm = Farm.objects.create(owner=owner, name="WP farm")
        grant(farm, owner)
        loc = FarmLocation.objects.create(farm=farm, name="WPLoc", latitude="46.250000",
                                          longitude="20.140000", timezone="Europe/Budapest")
        prop = FarmProperty.objects.create(farm=farm, location=loc, name="WPProp",
                                           kind="field", area_hectares=5)
        cyc = CropCycle.objects.create(farm=farm, property=prop, crop_name="WPBuza",
                                       state="current")
        before = WorkItem.objects.create(farm=farm, crop_cycle=cyc, property=prop,
                                         title="WPKeep", capability="tillage",
                                         start_date=date(2026, 3, 1), end_date=date(2026, 3, 5))
        worker = make_user("wp-w@pelda.hu")
        Membership.objects.create(farm=farm, user=worker, role="worker", is_active=True)
        run = ChatAttempt.objects.create(farm=farm, initiated_by=worker, idempotency_key="wp-1",
                                         language="hu", status="success", source="live",
                                         requested_model="m", user_message="terv?",
                                         answer_text="ok")
        prop_obj = ChatProposal.objects.create(attempt=run, farm=farm, position=1,
                                               kind="plans_generate", status="pending")
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(worker, farm, prop_obj.id)
        self.assertEqual(cm.exception.code, "forbidden")
        self.assertEqual(WorkItem.objects.filter(farm=farm).count(), 1)
        self.assertTrue(WorkItem.objects.filter(pk=before.pk).exists())
        prop_obj.refresh_from_db()
        self.assertEqual(prop_obj.status, "pending")


class MockContextEnrichmentTests(TestCase):
    def test_provider_payload_contains_domain_names(self):
        u = make_user("enr-o@pelda.hu")
        farm = Farm.objects.create(owner=u, name="Enr farm")
        grant(farm, u)
        loc = FarmLocation.objects.create(farm=farm, name="EnrLoc", latitude="46.250000",
                                          longitude="20.140000", timezone="Europe/Budapest")
        prop = FarmProperty.objects.create(farm=farm, location=loc, name="EnrPropDom",
                                           kind="field", area_hectares=5)
        Equipment.objects.create(farm=farm, name="EnrTractorDom", capabilities=["tillage"])
        CropCycle.objects.create(farm=farm, property=prop, crop_name="EnrBuzaDom",
                                 state="current")

        seen = {}

        class CapClient:
            def generate_chat(self, message, context, language=None):
                seen["context"] = context
                return {"answer": "ok", "actions": [], "links": [],
                        "_actual_model": "m"}

        from portal.models import Farm as FarmM
        _ctx, ctx_str = build_chat_context(FarmM.objects.get(pk=farm.pk))
        run = request_chat(u, farm, "mi van?", "enr-1", language="hu",
                           client=CapClient(), context_str=ctx_str)
        self.assertEqual(run.status, "success")
        for needle in ("EnrPropDom", "EnrTractorDom", "EnrBuzaDom"):
            self.assertIn(needle, seen.get("context", ""))
