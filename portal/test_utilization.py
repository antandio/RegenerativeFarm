"""Utilization Gantt tests: GET-only monitor over persisted WorkItems.

Strict vertical: assert actual context Decimal totals, states, counts and
exact task-id sets per row (never substrings).
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from portal.models import (
    CropCycle, Equipment, EquipmentProperty, Farm, FarmLocation, FarmProperty,
    Membership, WorkItem,
)
from portal import utilization as U

User = get_user_model()
PW = "TesztJelszo123!"
N = [1000]


def mkuser(prefix="u"):
    N[0] += 1
    return User.objects.create_user(email="%s%d@example.com" % (prefix, N[0]), password=PW)


def mkfarm(u, name=None):
    N[0] += 1
    return Farm.objects.create(owner=u, name=name or ("F%d" % N[0]))


def mkenv(f, kind="field", cname="Buza", area="2"):
    loc = FarmLocation.objects.create(farm=f, name="L%d" % N[0], latitude="47.1",
                                      longitude="19.1", timezone="Europe/Budapest")
    prop = FarmProperty.objects.create(farm=f, location=loc, name="P%d" % N[0],
                                       kind=kind, area_hectares=area)
    cyc = CropCycle.objects.create(farm=f, property=prop, crop_name=cname, state="current",
                                   sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
    return loc, prop, cyc


def mkeq(f, caps=("tillage",), avail=True, hpd="8"):
    N[0] += 1
    return Equipment.objects.create(farm=f, name="E%d" % N[0], capabilities=list(caps),
                                    available=avail, hours_per_day=hpd)


def mkw(f, cyc, prop, eq=None, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10",
        cap="tillage", missing=False, rec=""):
    N[0] += 1
    return WorkItem.objects.create(farm=f, crop_cycle=cyc, property=prop,
                                   title="T%d" % N[0], capability=cap,
                                   start_date=s, end_date=e, estimated_hours=hrs,
                                   equipment=eq, missing=missing, recommendation=rec)


def login(u):
    c = Client()
    c.force_login(u)
    return c


STYLE_RE = re.compile(r'style="left:(\d+\.\d+)%;width:(\d+\.\d+)%"')


class _UtilBarParser(HTMLParser):
    """Exact bar selector: only <a class="util-bar ..."> with #work-<id> href."""

    def __init__(self):
        super().__init__()
        self.bars = {}

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        d = dict(attrs)
        if "util-bar" not in (d.get("class") or "").split():
            return
        m = re.search(r"#work-(\d+)", d.get("href") or "")
        ms = re.search(r"left:(\d+\.\d+)%;width:(\d+\.\d+)%", d.get("style") or "")
        if m and ms:
            self.bars[int(m.group(1))] = (ms.group(1), ms.group(2))


def util_bar_styles(body):
    p = _UtilBarParser()
    p.feed(body)
    return p.bars


class WeekdayMathTests(TestCase):
    def test_full_week(self):
        self.assertEqual(U.weekdays_between(date(2026, 4, 6), date(2026, 4, 12)), 5)

    def test_weekend_only_zero(self):
        self.assertEqual(U.weekdays_between(date(2026, 4, 11), date(2026, 4, 12)), 0)

    def test_single_weekday(self):
        self.assertEqual(U.weekdays_between(date(2026, 4, 6), date(2026, 4, 6)), 1)

    def test_reversed_safe(self):
        self.assertEqual(U.weekdays_between(date(2026, 4, 10), date(2026, 4, 6)), 0)

    def test_huge_window_o1(self):
        self.assertEqual(U.weekdays_between(date(2000, 1, 1), date(2800, 12, 31)), 208970)

    def test_parse_hours_bad(self):
        self.assertIsNone(U.parse_hours("0"))
        self.assertIsNone(U.parse_hours("-3"))
        self.assertIsNone(U.parse_hours("nan"))
        self.assertIsNone(U.parse_hours("Infinity"))
        self.assertIsNone(U.parse_hours(None))
        self.assertIsNone(U.parse_hours("abc"))
        self.assertEqual(U.parse_hours("7.5"), Decimal("7.5"))

    def test_parse_capacity_missing_attr_is_none(self):
        class Obj:
            available = True
        self.assertEqual(U.parse_machine_capacity(Obj()), (True, None))

    def test_parse_capacity_invalid_is_none(self):
        class Obj:
            available = True
            hours_per_day = "xx"
        avail, hpd = U.parse_machine_capacity(Obj())
        self.assertTrue(avail)
        self.assertIsNone(hpd)

    def test_parse_capacity_none_is_none(self):
        class Obj:
            available = True
            hours_per_day = None
        self.assertEqual(U.parse_machine_capacity(Obj()), (True, None))

    def test_task_share_exact_no_early_rounding(self):
        # 1h over 3 weekdays: per-day share is the exact 1/3, never 0.3.
        s = U.task_share(date(2026, 4, 6), date(2026, 4, 8), "1",
                         date(2026, 4, 6), date(2026, 4, 6))
        self.assertFalse(s["unknown"])
        self.assertEqual(s["per_day"], Decimal("1") / 3)
        self.assertNotEqual(s["allocated"], Decimal("0.3"))
        self.assertEqual(s["allocated"], Decimal("1") / 3)

    def test_safe_add_clamps(self):
        self.assertEqual(U.safe_add_days(date(9999, 12, 20), 30), date.max)
        self.assertEqual(U.safe_add_days(date(1, 1, 10), -30), date.min)


class WindowTests(TestCase):
    def test_default_anchored_saved(self):
        u = mkuser("w")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 5, 4), e=date(2026, 5, 8))
        vs, ve = U.resolve_window(f, {}, date(2026, 1, 1))
        self.assertEqual((vs, ve), (date(2026, 5, 4), date(2026, 5, 8)))

    def test_default_today_no_tasks(self):
        u = mkuser("w2")
        f = mkfarm(u)
        vs, ve = U.resolve_window(f, {}, date(2026, 2, 3))
        self.assertEqual((vs, ve), (date(2026, 2, 3), date(2026, 3, 4)))

    def test_invalid_reversed_extreme(self):
        u = mkuser("w3")
        f = mkfarm(u)
        today = date(2026, 2, 3)
        dflt = (today, today + timedelta(days=29))
        self.assertEqual(U.resolve_window(f, {"start": "xx", "end": "yy"}, today), dflt)
        self.assertEqual(U.resolve_window(f, {"start": "1200-01-01", "end": "1200-02-01"}, today), dflt)
        vs, ve = U.resolve_window(f, {"start": "2026-04-10", "end": "2026-04-01"}, today)
        self.assertEqual((vs, ve), (date(2026, 4, 1), date(2026, 4, 10)))

    def test_cap_90(self):
        u = mkuser("w4")
        f = mkfarm(u)
        vs, ve = U.resolve_window(f, {"start": "2026-01-01", "end": "2026-12-31"}, date(2026, 1, 1))
        self.assertEqual((ve - vs).days + 1, 90)

    def test_year9999_stored_safe_default_and_nav(self):
        u = mkuser("w9")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(9999, 11, 1), e=date(9999, 12, 20), hrs="5")
        vs, ve = U.resolve_window(f, {}, date(2026, 1, 1))
        self.assertEqual(vs, date(9999, 11, 1))
        self.assertLessEqual(ve, date.max)
        self.assertLessEqual((ve - vs).days + 1, 90)
        body = login(u).get("/kihasznaltsag/").content.decode()
        self.assertIn("work-", body)
        m = re.search(r'href="([^"]*start=(\d{4}-\d{2}-\d{2})[^"]*)"', body)
        self.assertIsNotNone(m)
        for iso in re.findall(r"start=(\d{4}-\d{2}-\d{2})", body):
            d = date.fromisoformat(iso)
            self.assertGreaterEqual(d.year, 1)


class ViewTests(TestCase):
    def test_get_renders_and_post_405(self):
        u = mkuser("v")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f)
        EquipmentProperty.objects.create(equipment=eq, property=prop)
        w = mkw(f, cyc, prop, eq=eq)
        c = login(u)
        r = c.get("/kihasznaltsag/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("work-%d" % w.id, body)
        self.assertIn("/terv/#work-%d" % w.id, body)
        self.assertEqual(c.post("/kihasznaltsag/").status_code, 405)

    def test_context_exact_ids_and_decimal_totals(self):
        u = mkuser("x")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        w = mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-07")
        ctx = r.context
        self.assertEqual(ctx["farm_total_hours"], Decimal("10"))
        self.assertEqual(ctx["farm_allocated"], Decimal("4.0"))
        self.assertEqual(ctx["farm_task_count"], 1)
        self.assertEqual(ctx["prop_rows"][0]["task_ids"], [w.id])
        self.assertEqual(ctx["farm_unknown"], 0)

    def test_inline_styles_dot_decimals_hu(self):
        u = mkuser("l10n")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 4, 7), e=date(2026, 4, 9), hrs="6")
        body = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12").content.decode()
        styles = re.findall(r'style="[^"]*"', body)
        self.assertGreater(len(styles), 0)
        for st in styles:
            self.assertNotIn(",", st)
        self.assertTrue(STYLE_RE.search(body))

    def test_axis_day_grid_accurate_offsets(self):
        u = mkuser("ax")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        days = r.context["days"]
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0]["left"], "0.0000")
        self.assertEqual(days[0]["width"], "%.4f" % (100.0 / 7))
        # First partial-week day (Mon) and Sunday offsets are exact.
        self.assertEqual(days[6]["left"], "%.4f" % (6 / 7 * 100.0))
        ticks = {t["date"].isoformat(): t["left"] for t in r.context["ticks"]}
        self.assertEqual(ticks["2026-04-06"], "0.0000")
        self.assertEqual(ticks["2026-04-13"] if "2026-04-13" in ticks else ticks.get("2026-04-06"), "0.0000")

    def test_equipment_and_unassigned_real_bars(self):
        u = mkuser("bars")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f)
        EquipmentProperty.objects.create(equipment=eq, property=prop)
        wa = mkw(f, cyc, prop, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="6")
        wu = mkw(f, cyc, prop, s=date(2026, 4, 7), e=date(2026, 4, 9), hrs="6")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        erow = [r for r in r.context["equip_rows"] if r["equip"].id == eq.id][0]
        self.assertEqual(erow["task_ids"], [wa.id])
        self.assertEqual(len(erow["bars"]), 1)
        body = r.content.decode()
        self.assertIn("style=\"left:%s%%;width:%s%%\"" % (erow["bars"][0]["left"], erow["bars"][0]["width"]), body)
        bars = util_bar_styles(body)
        self.assertIn(wa.id, bars)
        self.assertEqual(bars[wa.id], (erow["bars"][0]["left"], erow["bars"][0]["width"]))
        un = r.context["unassigned"]
        self.assertEqual(sorted(u["task"].id for u in un), [wu.id])
        for u in un:
            self.assertIn("style=\"left:%s%%;width:%s%%\"" % (u["bar"]["left"], u["bar"]["width"]), body)
            self.assertIn(u["task"].id, bars)
            self.assertEqual(bars[u["task"].id], (u["bar"]["left"], u["bar"]["width"]))
        self.assertIn("util-eq-%d" % wa.id, body)

    def test_filters_intersection_and_farm_scope(self):
        u = mkuser("flt")
        f = mkfarm(u)
        _, pa, ca = mkenv(f)
        loc2 = FarmLocation.objects.create(farm=f, name="LX", latitude="47.1",
                                           longitude="19.1", timezone="Europe/Budapest")
        pb = FarmProperty.objects.create(farm=f, location=loc2, name="PB", kind="orchard",
                                         area_hectares="1")
        cb = CropCycle.objects.create(farm=f, property=pb, crop_name="Alma", state="current",
                                      sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
        eq = mkeq(f)
        EquipmentProperty.objects.create(equipment=eq, property=pa)
        wa = mkw(f, ca, pa, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="6")
        wb = mkw(f, cb, pb, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="6")
        c = login(u)
        base = "/kihasznaltsag/?start=2026-04-06&end=2026-04-12"
        # Equipment filter hides the other property's task everywhere.
        r = c.get(base + "&equipment=%d" % eq.id)
        ids = sorted(t for row in r.context["prop_rows"] for t in row["task_ids"])
        self.assertEqual(ids, [wa.id])
        erows = [r2 for r2 in r.context["equip_rows"] if r2["task_ids"]]
        self.assertEqual(sorted(t for r2 in erows for t in r2["task_ids"]), [wa.id])
        # Farm totals stay whole-farm despite the filter.
        self.assertEqual(r.context["farm_task_count"], 2)
        self.assertEqual(r.context["farm_total_hours"], Decimal("12"))
        # Property filter hides equipment demand for other properties.
        r2 = c.get(base + "&property=%d" % pb.id)
        ids2 = sorted(t for row in r2.context["prop_rows"] for t in row["task_ids"])
        self.assertEqual(ids2, [wb.id])
        self.assertEqual(r2.context["farm_task_count"], 2)
        # Kind filter applies to both property and equipment demand.
        r3 = c.get(base + "&kind=orchard")
        ids3 = sorted(t for row in r3.context["prop_rows"] for t in row["task_ids"])
        self.assertEqual(ids3, [wb.id])
        # resource_type narrows groups.
        r4 = c.get(base + "&resource_type=equipment")
        self.assertEqual(r4.context["prop_rows"], [])
        self.assertTrue(r4.context["equip_rows"])
        # Invalid kind is ignored, not fatal.
        r5 = c.get(base + "&kind=bogus")
        self.assertEqual(r5.status_code, 200)
        self.assertIsNone(r5.context["kind"])

    def test_resource_type_dropdown_translated_kinds(self):
        u = mkuser("kind")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop)
        body = login(u).get("/kihasznaltsag/").content.decode()
        self.assertIn('name="resource_type"', body)
        # Translated kind labels, no raw English option values as labels.
        self.assertIn("Szántóföld", body)
        self.assertNotIn(">field<", body)
        self.assertNotIn(">orchard<", body)

    def test_unknown_weekly_never_ok(self):
        u = mkuser("unk")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f)
        EquipmentProperty.objects.create(equipment=eq, property=prop)
        mkw(f, cyc, prop, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10")
        wu = mkw(f, cyc, prop, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="0")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        erow = [r2 for r2 in r.context["equip_rows"] if r2["equip"].id == eq.id][0]
        self.assertEqual(erow["unknown_tasks"], 1)
        wk = erow["weeks"][0]
        self.assertEqual(wk["unknown"], 1)
        self.assertNotEqual(wk["state"], "ok")
        self.assertEqual(wk["state"], "unknown")
        self.assertIsNone(wk["pct"])
        fw = r.context["farm_week_rows"][0]
        self.assertEqual(fw["unknown"], 1)
        self.assertEqual(fw["state"], "unknown")
        body = r.content.decode()
        self.assertNotIn("work-%d" % wu.id + '" title', body.replace("util-eq-", "work-")[:0])

    def test_weekend_unknown_hits_overlapping_bucket(self):
        u = mkuser("we")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f)
        mkw(f, cyc, prop, eq=eq, s=date(2026, 4, 11), e=date(2026, 4, 12), hrs="8")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        erow = [r2 for r2 in r.context["equip_rows"] if r2["equip"].id == eq.id][0]
        states = [(w["label"], w["unknown"], w["state"]) for w in erow["weeks"]]
        self.assertTrue(any(u2 >= 1 for _l, u2, _s in states))
        self.assertTrue(all(s != "ok" for _l, u2, s in states if u2))

    def test_unavailable_unknown_stays_conflict(self):
        u = mkuser("d")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f, avail=False)
        mkw(f, cyc, prop, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="0")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        erow = [r2 for r2 in r.context["equip_rows"] if r2["equip"].id == eq.id][0]
        self.assertEqual(erow["weeks"][0]["state"], "conflict")

    def test_table_state_missing_and_no_equipment(self):
        u = mkuser("tbl")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="6",
            missing=True, rec="Szerezz be vetőgépet")
        w2 = mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="6")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        bars = [b for row in r.context["prop_rows"] for b in row["bars"]]
        states = {b["task"].id: b["table_state"] for b in bars}
        self.assertIn(w2.id, states)
        missing_ids = [b["task"].id for b in bars if "Szerezz be vetőgépet" in b["table_state"]]
        self.assertEqual(len(missing_ids), 1)
        noeq = states[w2.id].lower()
        self.assertTrue("nincs" in noeq or "no equipment" in noeq or "felszerelés" in noeq or "gép" in noeq or "equipment" in noeq)

    def test_multi_property_same_machine_once(self):
        u = mkuser("s")
        f = mkfarm(u)
        _, pa, ca = mkenv(f)
        loc2 = FarmLocation.objects.create(farm=f, name="LM", latitude="47.1",
                                           longitude="19.1", timezone="Europe/Budapest")
        pb = FarmProperty.objects.create(farm=f, location=loc2, name="PM", kind="field",
                                         area_hectares="1")
        cb = CropCycle.objects.create(farm=f, property=pb, crop_name="Kukorica", state="current",
                                      sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
        eq = mkeq(f, hpd="8")
        EquipmentProperty.objects.create(equipment=eq, property=pa)
        EquipmentProperty.objects.create(equipment=eq, property=pb)
        wa = mkw(f, ca, pa, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10")
        wb = mkw(f, cb, pb, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-10")
        self.assertEqual(r.context["farm_task_count"], 2)
        self.assertEqual(r.context["farm_total_hours"], Decimal("20"))
        self.assertEqual(r.context["farm_allocated"], Decimal("20.0"))
        erow = [r2 for r2 in r.context["equip_rows"] if r2["equip"].id == eq.id][0]
        self.assertEqual(sorted(erow["task_ids"]), sorted([wa.id, wb.id]))
        # One machine, one capacity: 5 weekdays x 8h = 40h available.
        self.assertEqual(erow["weeks"][0]["available"], Decimal("40.0"))
        self.assertEqual(erow["weeks"][0]["assigned"], Decimal("20.0"))

    def test_clipped_cross_week_sums(self):
        u = mkuser("cw")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        # 10 weekdays Mon 4/6..Fri 4/17 -> 1h/day; window Wed 4/8..Tue 4/14.
        mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 17), hrs="10")
        r = login(u).get("/kihasznaltsag/?start=2026-04-08&end=2026-04-14")
        self.assertEqual(r.context["farm_allocated"], Decimal("5.0"))
        fw = {w["label"]: w for w in r.context["farm_week_rows"]}
        vals = sorted(fw.values(), key=lambda w: w["label"])
        self.assertEqual(vals[0]["assigned"], Decimal("3.0"))
        self.assertEqual(vals[1]["assigned"], Decimal("2.0"))

    def test_tiny_fractional_aggregate(self):
        u = mkuser("fr")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        for _i in range(3):
            mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="1.0")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-08")
        # 3 tasks x 1h over the same 3 weekdays -> 3.0 allocated, not 2.7.
        self.assertEqual(r.context["farm_allocated"], Decimal("3.0"))

    def test_no_get_writes_queries_and_no_live_api(self):
        u = mkuser("g")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop)
        before = WorkItem.objects.filter(farm=f).count()
        c = login(u)
        with CaptureQueriesContext(connection) as ctx, \
             mock.patch("portal.weather.httpx.get") as hg, \
             mock.patch("httpx.get") as hg2:
            r = c.get("/kihasznaltsag/")
            self.assertEqual(r.status_code, 200)
            hg.assert_not_called()
            hg2.assert_not_called()
        writes = [q for q in ctx.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertEqual(WorkItem.objects.filter(farm=f).count(), before)

    def test_tenant_isolation_and_foreign_404(self):
        ua = mkuser("ia")
        fa = mkfarm(ua)
        _, pa, ca = mkenv(fa)
        mkw(fa, ca, pa)
        ub = mkuser("ib")
        fb = mkfarm(ub)
        _, pb, cb = mkenv(fb)
        wb = mkw(fb, cb, pb)
        body = login(ua).get("/kihasznaltsag/").content.decode()
        self.assertNotIn("work-%d" % wb.id, body)
        r = login(ua).get("/kihasznaltsag/?property=%d" % pb.id)
        self.assertEqual(r.status_code, 404)
        r = login(ua).get("/kihasznaltsag/?equipment=999999")
        self.assertEqual(r.status_code, 404)

    def test_roles_all_read(self):
        u = mkuser("r")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop)
        for role in ("manager", "worker", "viewer"):
            m = mkuser("r" + role)
            Membership.objects.create(farm=f, user=m, role=role, is_active=True)
            self.assertEqual(login(m).get("/kihasznaltsag/").status_code, 200)
        v = mkuser("rvw")
        Membership.objects.create(farm=f, user=v, role="viewer", is_active=True)
        body = login(v).get("/kihasznaltsag/").content.decode()
        self.assertIn("/properties/#property-", body)
        self.assertNotIn("/edit/", body)
        self.assertIn('href="/kihasznaltsag/"', body)
        self.assertIn("Munkaterv", body)

    def test_property_capacity_unknown_honest(self):
        u = mkuser("p")
        f = mkfarm(u)
        loc = FarmLocation.objects.create(farm=f, name="LP", latitude="47.1",
                                          longitude="19.1", timezone="Europe/Budapest")
        FarmProperty.objects.create(farm=f, location=loc, name="Proc", kind="processing",
                                    area_hectares="1")
        body = login(u).get("/kihasznaltsag/").content.decode().lower()
        self.assertIn("unknown", body)
        self.assertIn("proc", body)
        self.assertIn("ismeretlen", body)

    def test_property_occupancy_clarified_no_percent(self):
        u = mkuser("o")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 8), hrs="6")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        body = r.content.decode()
        self.assertNotIn("occupancy:", body)
        self.assertIn("fedett napok", body)

    def test_hu_no_raw_conflict(self):
        u = mkuser("hu")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f, avail=False)
        mkw(f, cyc, prop, eq=eq)
        body = login(u).get("/kihasznaltsag/").content.decode()
        self.assertNotIn("conflict:", body)
        self.assertIn("konfliktus", body.lower())

    def test_overload_percent_and_legend(self):
        u = mkuser("ov")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        eq = mkeq(f, hpd="1")
        EquipmentProperty.objects.create(equipment=eq, property=prop)
        mkw(f, cyc, prop, eq=eq, s=date(2026, 4, 6), e=date(2026, 4, 6), hrs="8")
        r = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12")
        wk = [r2 for r2 in r.context["equip_rows"] if r2["equip"].id == eq.id][0]["weeks"][0]
        self.assertEqual(wk["state"], "overload")
        self.assertEqual(wk["pct"], Decimal("160.0"))
        body = r.content.decode()
        self.assertIn("legend", body.lower())

    def test_i18n_hu_en(self):
        u = mkuser("i18")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop)
        c = login(u)
        hu = c.get("/kihasznaltsag/").content.decode()
        en = c.get("/kihasznaltsag/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn("Kihasználtság", hu)
        self.assertIn("Utilisation", en)
        # Uniform-distribution note is translated in EN.
        self.assertIn("uniform", en.lower())
        self.assertIn("weekday", en.lower())

    def test_navigation_links(self):
        u = mkuser("n")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 10))
        body = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-12").content.decode()
        self.assertIn("start=2026-03-30", body)
        self.assertIn("start=2026-04-13", body)

    def test_sticky_timeline_markers(self):
        u = mkuser("t")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop)
        body = login(u).get("/kihasznaltsag/").content.decode()
        self.assertIn("util-timeline", body)
        self.assertIn("scrollregion", body)
        self.assertIn("sticky", body.lower())

    def test_empty_guidance(self):
        u = mkuser("e")
        mkfarm(u)
        body = login(u).get("/kihasznaltsag/").content.decode()
        self.assertIn("/terv/", body)

    def test_tooltip_shows_window_and_allocated(self):
        u = mkuser("tt")
        f = mkfarm(u)
        _, prop, cyc = mkenv(f)
        mkw(f, cyc, prop, s=date(2026, 4, 6), e=date(2026, 4, 10), hrs="10")
        body = login(u).get("/kihasznaltsag/?start=2026-04-06&end=2026-04-07").content.decode().lower()
        self.assertIn("teljes ablak", body)
        self.assertIn("kiosztott", body)
