"""RED regression: plans GET summary must come from persisted WorkItems."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import CropCycle, Equipment, Farm, FarmLocation, FarmProperty, WorkItem

User = get_user_model()
PW = "TesztJelszo123!"


def _mk_farm(u, name="RF"):
    f = Farm.objects.create(owner=u, name=name)
    loc = FarmLocation.objects.create(
        farm=f, name="RL" + name, latitude="47.1", longitude="19.1",
        timezone="Europe/Budapest")
    prop = FarmProperty.objects.create(
        farm=f, location=loc, name="RP" + name, kind="field", area_hectares="2")
    cyc = CropCycle.objects.create(
        farm=f, property=prop, crop_name="Buza", state="current",
        sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
    return f, prop, cyc


class PlansPersistedSummaryTests(TestCase):
    def test_get_summary_equals_saved_workitem_not_ephemeral(self):
        u = User.objects.create_user(email="rec@example.com", password=PW)
        f = Farm.objects.create(owner=u, name="RF")
        loc = FarmLocation.objects.create(
            farm=f, name="RL", latitude="47.1", longitude="19.1",
            timezone="Europe/Budapest")
        prop = FarmProperty.objects.create(
            farm=f, location=loc, name="RP", kind="field", area_hectares="2")
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Buza", state="current",
            sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
        s, e = date(2026, 4, 6), date(2026, 4, 10)
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Saved36",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="36", equipment=None, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        with mock.patch("portal.plans_views.generate_plan", return_value=([],{'week_peaks':{}})) as gp:
            r = self.client.get("/terv/")
            gp.assert_not_called()
        self.assertEqual(r.status_code, 200)
        summary = r.context["weather_summary"]
        wk = (s.isocalendar()[0], s.isocalendar()[1])
        self.assertIn(wk, summary["week_peaks"])
        self.assertEqual(summary["week_peaks"][wk], Decimal("36"))


class WeekendUnknownTests(TestCase):
    """Weekend-only / unknown demand must NOT contribute numeric hours,
    but must still render (never disappear when another item shares week)."""

    def test_weekend_only_standalone_contributes_zero(self):
        u = User.objects.create_user(email="we@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "WE")
        s, e = date(2026, 4, 11), date(2026, 4, 12)  # Sat-Sun
        wi = WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Weekend10",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="10", equipment=None, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        self.assertEqual(r.status_code, 200)
        summary = r.context["weather_summary"]
        wk = (s.isocalendar()[0], s.isocalendar()[1])
        self.assertTrue(wk not in summary["week_peaks"]
                        or summary["week_peaks"][wk] == Decimal("0"))
        copies = [c for _wk, lst in r.context["timeline"] for c in lst]
        self.assertTrue(any(c.id == wi.id for c in copies))
        lone = [c for _wk, lst in r.context["timeline"] for c in lst
                if c.id == wi.id][0]
        self.assertEqual(lone.allocated_hours, Decimal("0"))

    def test_weekend_only_not_hidden_by_weekday_item_same_week(self):
        u = User.objects.create_user(email="we2@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "WE2")
        s, e = date(2026, 4, 6), date(2026, 4, 10)  # Mon-Fri
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Weekday10",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="10", equipment=None, missing=False,
            recommendation="", guidance="")
        ws, we = date(2026, 4, 11), date(2026, 4, 12)  # Sat-Sun same week
        wi = WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Weekend10",
            capability="tillage", start_date=ws, end_date=we,
            estimated_hours="10", equipment=None, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        self.assertEqual(r.status_code, 200)
        summary = r.context["weather_summary"]
        wk = (s.isocalendar()[0], s.isocalendar()[1])
        self.assertEqual(summary["week_peaks"][wk], Decimal("10"))
        copies = [c for _wk, lst in r.context["timeline"] for c in lst
                  if c.id == wi.id]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].allocated_hours, Decimal("0"))


class EquipmentDuplicateTests(TestCase):
    """Same equipment names may duplicate: ID internally, names display only."""

    def _mk_eq(self, f, name, hpd):
        return Equipment.objects.create(
            farm=f, name=name, capabilities=["tillage"],
            available=True, hours_per_day=hpd)

    def test_same_name_not_merged_capacity_per_id(self):
        u = User.objects.create_user(email="eq@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "EQ")
        eq1 = self._mk_eq(f, "Traktor", "6")  # weekly cap 30
        eq2 = self._mk_eq(f, "Traktor", "4")  # weekly cap 20
        s, e = date(2026, 4, 6), date(2026, 4, 10)
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Job1",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="25", equipment=eq1, missing=False,
            recommendation="", guidance="")
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Job2",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="25", equipment=eq2, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        summary = r.context["weather_summary"]
        wk = (s.isocalendar()[0], s.isocalendar()[1])
        k1, k2 = (wk[0], wk[1], eq1.id), (wk[0], wk[1], eq2.id)
        self.assertEqual(summary["equipment_peaks"][k1], Decimal("25"))
        self.assertEqual(summary["equipment_peaks"][k2], Decimal("25"))
        self.assertIn(k2, summary["over_equipment_weeks"])
        self.assertNotIn(k1, summary["over_equipment_weeks"])

    def test_no_invented_eight_fallback(self):
        u = User.objects.create_user(email="eq2@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "EQ2")
        eq = self._mk_eq(f, "Eke", "6")  # weekly cap 30, not 40
        s, e = date(2026, 4, 6), date(2026, 4, 10)
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Job",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="32", equipment=eq, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        summary = r.context["weather_summary"]
        wk = (s.isocalendar()[0], s.isocalendar()[1])
        self.assertIn((wk[0], wk[1], eq.id),
                      summary["over_equipment_weeks"])


class WeeklyIterationTests(TestCase):
    """Weekly stepping (timedelta 7), date.max safe, via helper module."""

    def test_iter_week_keys_spans_weeks(self):
        from portal.plan_summary import iter_week_keys
        keys = iter_week_keys(date(2026, 4, 6), date(2026, 4, 20))
        self.assertEqual(keys, [(2026, 15), (2026, 16), (2026, 17)])

    def test_iter_week_keys_date_max_safe(self):
        from portal.plan_summary import iter_week_keys
        from datetime import date as dcls
        keys = iter_week_keys(dcls(9999, 12, 25), dcls.max)
        self.assertTrue(len(keys) >= 1)
        self.assertEqual(keys[-1], (9999, 52))


class CrossWeekTemplateTests(TestCase):
    """Timeline must show allocated_hours + total, unique anchors."""

    def test_crossweek_allocated_and_unique_anchors(self):
        u = User.objects.create_user(email="xw@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "XW")
        s, e = date(2026, 4, 6), date(2026, 4, 17)  # 10 weekdays
        wi = WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Cross30",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="30", equipment=None, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        self.assertEqual(r.status_code, 200)
        tl = dict(r.context["timeline"])
        self.assertEqual(len(tl), 2)
        allocs = sorted(c.allocated_hours
                        for lst in tl.values() for c in lst if c.id == wi.id)
        self.assertEqual(allocs, [Decimal("15"), Decimal("15")])
        html = r.content.decode()
        self.assertEqual(html.count('id="work-%d"' % wi.id), 1)
        self.assertIn("15", html)
        self.assertIn("30", html)


class SummaryCrossCheckTests(TestCase):
    """Ten fixtures: summary equals utilization.task_share, Decimal exact."""

    FIXTURES = [
        (date(2026, 4, 6), date(2026, 4, 10), "10"),
        (date(2026, 4, 13), date(2026, 4, 17), "25"),
        (date(2026, 4, 20), date(2026, 5, 1), "20"),
        (date(2026, 5, 4), date(2026, 5, 8), "36"),
        (date(2026, 5, 9), date(2026, 5, 10), "10"),
        (date(2026, 5, 11), date(2026, 5, 22), "30"),
        (date(2026, 5, 23), date(2026, 5, 24), "8"),
        (date(2026, 5, 25), date(2026, 5, 25), "4"),
        (date(2026, 6, 1), date(2026, 6, 12), "50"),
        (date(2026, 6, 13), date(2026, 6, 14), "12"),
    ]

    def test_ten_fixtures_match_task_share(self):
        from portal.utilization import task_share
        from portal.plan_summary import iter_week_keys
        u = User.objects.create_user(email="xc@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "XC")
        for i, (s, e, h) in enumerate(self.FIXTURES):
            with self.subTest(fixture=i):
                WorkItem.objects.create(
                    farm=f, crop_cycle=cyc, property=prop,
                    title="Fx%d" % i, capability="tillage",
                    start_date=s, end_date=e, estimated_hours=h,
                    equipment=None, missing=False,
                    recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        summary = r.context["weather_summary"]
        expected = {}
        exp_unknown = 0
        for i, (s, e, h) in enumerate(self.FIXTURES):
            with self.subTest(fixture=i):
                probe_weeks = iter_week_keys(s, e)
                any_alloc = False
                for wk in probe_weeks:
                    ws = date.fromisocalendar(wk[0], wk[1], 1)
                    we = date.fromisocalendar(wk[0], wk[1], 7)
                    sh = task_share(s, e, Decimal(h), ws, we)
                    if sh["unknown"] or sh["allocated"] is None:
                        continue
                    if sh["allocated"] == 0:
                        continue
                    any_alloc = True
                    expected[wk] = expected.get(wk, Decimal("0")) + sh["allocated"]
                if not any_alloc:
                    exp_unknown += 1
        for wk, hrs in expected.items():
            with self.subTest(week=wk):
                self.assertEqual(summary["week_peaks"].get(wk), hrs)
        self.assertEqual(summary["weekend_unknown_count"], exp_unknown)
        self.assertEqual(exp_unknown, 3)


class OverloadOptionsTests(TestCase):
    """Two neutral overload options, no recommendation inference."""

    def test_two_neutral_options_on_overload(self):
        u = User.objects.create_user(email="ov@example.com", password=PW)
        f, prop, cyc = _mk_farm(u, "OV")
        s, e = date(2026, 4, 6), date(2026, 4, 10)
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Big50",
            capability="tillage", start_date=s, end_date=e,
            estimated_hours="50", equipment=None, missing=False,
            recommendation="", guidance="")
        self.client.force_login(u)
        r = self.client.get("/terv/")
        summary = r.context["weather_summary"]
        opts = summary.get("overload_options")
        self.assertIsNotNone(opts)
        self.assertEqual(len(opts), 2)
        for o in opts:
            low = str(o).casefold()
            self.assertNotIn("javasolt", low)
            self.assertNotIn("ajánlott", low)
            self.assertNotIn("recommend", low)
        html = r.content.decode()
        self.assertIn(str(opts[0]), html)
        self.assertIn(str(opts[1]), html)
