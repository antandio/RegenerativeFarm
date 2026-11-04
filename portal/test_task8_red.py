"""Task8 functional RED: targeted failing specs for planning fix."""
from datetime import date
from decimal import Decimal
from django.test import TestCase
from portal.planning import generate_plan


def C(cid, pid, name, state="current", growth="", sow=None, end=None, obs="", area="1"):
    return {"id": cid, "property_id": pid, "crop_name": name, "state": state,
            "growth_stage": growth, "sow_date": sow, "expected_end_date": end,
            "observations": obs, "area_hectares": area}


def EQ(eid, caps, avail=True, props=None, hpd="8"):
    return {"id": eid, "capabilities": caps, "available": avail,
            "property_ids": props or [], "hours_per_day": hpd}


class Task8RedTests(TestCase):
    def test_unassigned_capable_means_missing(self):
        c = C(1, 10, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1))
        eq = [EQ(5, ["tillage", "sowing", "irrigation", "harvest"], True, [999])]
        items, _ = generate_plan([c], eq)
        self.assertTrue(all(i["missing"] for i in items), "unassigned equipment must be missing")
        self.assertTrue(all(i["equipment_id"] is None for i in items))

    def test_history_produces_no_work(self):
        c = C(1, 10, "Búza", state="history", sow=date(2026, 3, 1), end=date(2026, 7, 1))
        eq = [EQ(5, ["tillage", "sowing", "irrigation", "harvest"], True, [10])]
        items, _ = generate_plan([c], eq)
        self.assertEqual(items, [])

    def test_growing_pasture_differs_from_planned_cereal(self):
        past = C(1, 10, "Legelő gyep", state="current", growth="növekedés",
                 sow=date(2026, 3, 1), end=date(2026, 9, 1))
        cer = C(2, 11, "Búza", state="planned", sow=date(2026, 10, 1), end=date(2027, 7, 1))
        items_p, _ = generate_plan([past], [])
        items_c, _ = generate_plan([cer], [])
        self.assertTrue(len(items_p) > 0 and len(items_c) > 0)
        tp = {(i["title"], i["capability"]) for i in items_p}
        tc = {(i["title"], i["capability"]) for i in items_c}
        self.assertNotEqual(tp, tc, "pasture growing must differ from planned cereal")

    def test_area_scaling_hours(self):
        c1 = C(1, 10, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1), area="1")
        c2 = C(2, 11, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1), area="3")
        a, _ = generate_plan([c1], [])
        b, _ = generate_plan([c2], [])
        ha = sum((i["estimated_hours"] for i in a), Decimal("0"))
        hb = sum((i["estimated_hours"] for i in b), Decimal("0"))
        self.assertTrue(hb > ha, "larger area must scale hours")

    def test_same_isoweek_different_years_separated(self):
        c1 = C(1, 10, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1))
        c2 = C(2, 11, "Búza", sow=date(2027, 3, 1), end=date(2027, 7, 1))
        _, s = generate_plan([c1, c2], [])
        peaks = s["week_peaks"]
        # keys must encode year (tuple or YYYY-Www string), not bare int
        for k in peaks:
            self.assertFalse(isinstance(k, int), "week key must separate years: %r" % (k,))
        # same ISO week across years must not collapse: total keys > single-cycle keys
        _, s1 = generate_plan([c1], [])
        self.assertTrue(len(peaks) >= len(s1["week_peaks"]))

    def test_shared_equipment_peak_uses_unique_capacity(self):
        # one 8h/day machine shared by 2 properties, heavy same-week demand
        c1 = C(1, 10, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1), area="5")
        c2 = C(2, 10, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1), area="5")
        eq = [EQ(7, ["tillage", "sowing", "irrigation", "harvest"], True, [10], "8")]
        items, s = generate_plan([c1, c2], eq)
        self.assertIn("equipment_peaks", s)
        # daily 8h * 5 = 40h weekly cap per unique equipment; demand should exceed
        overload = s.get("over_equipment_weeks") or s.get("equipment_overload") or []
        self.assertTrue(len(overload) > 0, "shared equipment demand must flag overload, got %r" % (s,))

    def test_summary_capacity_shown(self):
        c = C(1, 10, "Búza", sow=date(2026, 3, 1), end=date(2026, 7, 1))
        _, s = generate_plan([c], [])
        self.assertIn("week_capacity_hours", s)
        self.assertEqual(str(s["week_capacity_hours"]), "40")
        note = (s.get("capacity_note") or s.get("capacity_assumption") or "")
        self.assertTrue(note, "capacity assumption note must be present")
