"""Stage B UX trust/nav/accessibility slices — RED-first.

Covers: UX01 header contrast (actual CSS), UX05 safe chat nav,
UX17 lang labels, UX08 table regions, UX07 scroll hints,
UX10 collapsible filters, UX09 full-title links, UX14 targets,
UX04 human equipment rows, UX28 neutral options on plans+util,
CSS cache-bust version.
"""
import pathlib
import re
from datetime import date, timedelta
from decimal import Decimal
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from portal.chat import NAV_LINKS
from portal.domain_models import CropCycle, Equipment, FarmLocation, FarmProperty
from portal.models import Farm, Membership

User = get_user_model()
PW = "TesztJelszo123!"
CSS_VERSION = "v=20261104-knowledge-tables"

CSS_PATH = pathlib.Path(__file__).parent / "static/portal/app.css"

# POST-only / download routes that must never render as direct <a href>.
UNSAFE_PATHS = ("/tagok/meghivas/", "/weather/refresh/", "/teendok/uj/",
                 "/fiok/hozzajarulas/", "/fiok/export/")
SAFE_DESTS = ("/tagok/#invite", "/weather/#refresh", "/teendok/#new",
              "/fiok/#consent", "/fiok/")


def owner_client(email="stageb@example.com"):
    u = User.objects.create_user(email=email, password=PW)
    f = Farm.objects.create(owner=u, name="StageB Farm")
    Membership.objects.create(farm=f, user=u, role="owner", is_active=True)
    c = Client()
    c.force_login(u)
    return u, f, c


def seed_overload(f):
    loc = FarmLocation.objects.create(farm=f, name="L1", latitude=Decimal("46"),
                                      longitude=Decimal("20"), timezone="UTC")
    prop = FarmProperty.objects.create(farm=f, location=loc, name="P1",
                                       kind="field", area_hectares=Decimal("5"))
    eq = Equipment.objects.create(farm=f, name="Traktor", capabilities=["sowing"],
                                  available=True, hours_per_day=Decimal("1"))
    eq.properties.add(prop)
    cyc = CropCycle.objects.create(farm=f, property=prop, crop_name="Buza",
                                   state="planned")
    from portal.domain_models import WorkItem
    start = date.today() - timedelta(days=1)
    for i in range(6):
        WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Munka %d" % i,
            capability="sowing", start_date=start, end_date=start + timedelta(days=6),
            estimated_hours=Decimal("20"), equipment=eq)
    return prop, eq


class _ButtonParser(HTMLParser):
    """Collect (aria-label, visible text) for every full <button> element."""

    def __init__(self):
        super().__init__()
        self.buttons = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self._cur = [dict(attrs).get("aria-label", ""), ""]

    def handle_data(self, data):
        if self._cur is not None:
            self._cur[1] += data

    def handle_endtag(self, tag):
        if tag == "button" and self._cur is not None:
            self.buttons.append((self._cur[0], self._cur[1]))
            self._cur = None


def _assert_scroll_region(testcase, attrs, url):
    testcase.assertIn('tabindex="0"', attrs, url)
    testcase.assertIn('role="region"', attrs, url)
    m = re.search(r'aria-label="([^"]+)"', attrs)
    testcase.assertIsNotNone(m, url + " " + attrs)
    testcase.assertTrue(len(m.group(1)) > 3, url)


def _rel_lum(rgb):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _hex(s):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


class UX01HeaderContrastTests(TestCase):
    def test_top_muted_override_after_global_with_contrast(self):
        css = CSS_PATH.read_text()
        gpos = css.find(".muted {")
        self.assertNotEqual(gpos, -1, "missing global .muted rule")
        m = re.search(r"\.top\s+\.muted[^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "missing .top .muted override")
        self.assertGreater(css.find(".top .muted"), gpos,
                           ".top .muted must come after global .muted")
        fg = re.search(r"color\s*:\s*(#[0-9a-fA-F]{3,6})", m.group(1))
        self.assertIsNotNone(fg, "override needs explicit color")
        top = re.search(r"\.top\s*\{([^}]*)\}", css)
        bg = re.search(r"background\s*:\s*(#[0-9a-fA-F]{3,6}|var\(--forest\))",
                       top.group(1))
        self.assertIsNotNone(bg, "need .top background")
        bghex = "#17452A" if "var(" in bg.group(1) else bg.group(1)
        self.assertGreaterEqual(_contrast(_hex(fg.group(1)), _hex(bghex)), 4.5)

    def test_top_farm_contrast(self):
        css = CSS_PATH.read_text()
        m = re.search(r"\.top-farm\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "missing .top-farm rule")
        fg = re.search(r"color\s*:\s*(#[0-9a-fA-F]{3,6})", m.group(1))
        self.assertIsNotNone(fg, "top-farm needs explicit color")
        self.assertGreaterEqual(_contrast(_hex(fg.group(1)), _hex("#17452A")), 4.5)


class UX05SafeNavTests(TestCase):
    def test_nav_links_keys_preserved_for_validation(self):
        for key in ("invite", "weather_refresh", "action_create", "consent",
                    "export"):
            self.assertIn(key, NAV_LINKS)

    def test_chat_renders_zero_unsafe_hrefs_with_safe_dests(self):
        # Intentional task2 removal: no page-local nav card; the global
        # sidebar keeps GET-safe hrefs and typed navigate proposals cover
        # the POST-only destinations via their safe in-page dests.
        from portal.chat import CHAT_SAFE_DESTS, validate_chat_parsed
        from portal.chat_models import ChatAttempt, ChatProposal
        u, f, c = owner_client()
        b = c.get(reverse("chat")).content.decode()
        for raw in UNSAFE_PATHS:
            self.assertNotIn('href="%s"' % raw, b)
        self.assertNotIn("data-fallback", b)
        for key in ("dashboard", "farm", "location_list", "property_list",
                    "equipment_list", "cycle_list", "plans", "weather",
                    "actions", "account", "chat"):
            self.assertIn('href="%s"' % reverse(key), b, key)
        attempt = ChatAttempt.objects.create(
            farm=f, initiated_by=u, idempotency_key="ux05nav1",
            language="hu", status="success", source="live",
            requested_model="test", user_message="hova?",
            answer_text="Itt vannak a hivatkozasok.")
        pos = 0
        for link in ("invite", "weather_refresh", "action_create",
                     "consent", "export"):
            _, actions, _ = validate_chat_parsed(
                {"answer": "ok", "actions": [{"kind": "navigate", "link": link}],
                 "links": []})
            self.assertEqual(actions, [{"kind": "navigate", "link": link}])
            pos += 1
            ChatProposal.objects.create(
                attempt=attempt, farm=f, position=pos,
                kind="navigate", payload_link=link)
        b2 = c.get(reverse("chat")).content.decode()
        for dest in SAFE_DESTS:
            self.assertIn(dest, b2)

    def test_chat_no_snake_case_visible_collapsed(self):
        _, _, c = owner_client()
        b = c.get(reverse("chat")).content.decode()
        for key in NAV_LINKS:
            self.assertNotIn(">%s<" % key, b)
        self.assertIn("<details", b)

    def test_all_rendered_chat_links_get_safe(self):
        # Task2: static per-module hrefs live in the global sidebar, not in
        # a chat-local card; non-sidebar destinations are reached via typed
        # navigate proposals instead of static hrefs.
        _, _, c = owner_client()
        b = c.get(reverse("chat")).content.decode()
        hrefs = sorted(set(re.findall(r'href="(/[^"]*)"', b)))
        chat_hrefs = [h for h in hrefs if h.startswith(
            ("/attekintes", "/gazdasagom", "/locations", "/properties",
             "/equipment", "/cycles", "/tagok", "/terv", "/weather",
             "/tanacs", "/teendok", "/fiok", "/chat"))]
        self.assertGreaterEqual(len(chat_hrefs), 10)
        for h in chat_hrefs:
            base = h.split("#")[0] or "/"
            r = c.get(base)
            self.assertNotEqual(r.status_code, 405, h)


class UX17LangLabelTests(TestCase):
    def test_lang_buttons_aria_contains_visible_text_magyar_hu_english_en(self):
        _, _, c = owner_client()
        b = c.get(reverse("dashboard")).content.decode()
        p = _ButtonParser()
        p.feed(b)
        self.assertGreater(len(p.buttons), 0, "no <button> in actual DOM")
        for visible in ("Magyar HU", "English EN"):
            matches = [aria for aria, text in p.buttons
                       if text.strip() == visible]
            self.assertTrue(
                matches,
                "no <button> with visible text %r (actual DOM: %r)"
                % (visible, p.buttons))
            for aria in matches:
                self.assertIn(visible, aria)


class UX08TableRegionTests(TestCase):
    def test_every_table_scroll_is_region_with_label(self):
        _, f, c = owner_client()
        seed_overload(f)
        for url in (reverse("plans"), reverse("utilization")):
            b = c.get(url).content.decode()
            divs = re.findall(r'<div class="table-scroll[^"]*"([^>]*)>', b)
            self.assertGreater(len(divs), 0, url)
            for attrs in divs:
                _assert_scroll_region(self, attrs, url)

    def test_weather_only_present_scrolls_are_labeled_no_cache(self):
        # Missing cache renders no tables: assert actual state (location
        # present, entries rendered) and label only the scrolls present.
        _, f, c = owner_client()
        seed_overload(f)
        b = c.get(reverse("weather")).content.decode()
        self.assertNotIn("Nincs helyszín", b)
        for attrs in re.findall(r'<div class="table-scroll[^"]*"([^>]*)>', b):
            _assert_scroll_region(self, attrs, reverse("weather"))

    def test_weather_cached_tables_are_labeled_regions(self):
        import httpx
        from django.core.cache import cache
        from portal import weather as W
        _, f, c = owner_client()
        prop, _eq = seed_overload(f)
        loc = prop.location
        cache.clear()
        try:
            body = {"current": {"time": "2026-01-01T12:00", "temperature_2m": 5.0,
                                "precipitation": 0.2, "wind_speed_10m": 12.0},
                    "current_units": {"temperature_2m": "°C", "precipitation": "mm",
                                      "wind_speed_10m": "km/h"},
                    "daily": {"time": ["2026-01-0%d" % i for i in range(1, 8)],
                              "temperature_2m_max": [6.0] * 7,
                              "temperature_2m_min": [-1.0] * 7,
                              "precipitation_sum": [0.5] * 7,
                              "wind_speed_10m_max": [15.0] * 7}}
            W.refresh_forecast(
                f.id, loc,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=body)))
            b = c.get(reverse("weather")).content.decode()
            divs = re.findall(r'<div class="table-scroll[^"]*"([^>]*)>', b)
            self.assertGreater(len(divs), 0, "cached forecast must render tables")
            for attrs in divs:
                _assert_scroll_region(self, attrs, reverse("weather"))
        finally:
            cache.clear()


class UX07ScrollHintTests(TestCase):
    def test_visible_hints_sticky_firstcol_percent(self):
        _, f, c = owner_client()
        seed_overload(f)
        css = CSS_PATH.read_text()
        self.assertIn("scroll-hint", css)
        self.assertRegex(css, r"th:first-child|td:first-child")
        self.assertIn("sticky", css)
        for url in (reverse("plans"), reverse("utilization")):
            b = c.get(url).content.decode()
            self.assertIn("scroll-hint", b)
        bu = c.get(reverse("utilization")).content.decode()
        self.assertTrue("Százalék" in bu or "Percent" in bu)


class UX10FilterCollapseTests(TestCase):
    def test_filters_collapsible_summary_first(self):
        _, _, c = owner_client()
        b = c.get(reverse("utilization")).content.decode()
        m = re.search(r'<details[^>]*>\s*<summary>[^<]*</summary>\s*<form[^>]*class="util-filters"', b)
        self.assertIsNotNone(m, "filters must be in details>summary first")


class UX09FullTitleTests(TestCase):
    def test_bars_have_full_title_and_table_links(self):
        _, f, c = owner_client()
        seed_overload(f)
        b = c.get(reverse("utilization")).content.decode()
        bars = re.findall(r'<a[^>]*class="util-bar[^"]*"[^>]*>', b)
        self.assertGreater(len(bars), 0)
        for bar in bars:
            m = re.search(r'aria-label="([^"]+)"', bar)
            self.assertIsNotNone(m, bar)
            self.assertIn("Munka", m.group(1))
        bp = c.get(reverse("plans")).content.decode()
        self.assertIn("#work-", bp)


class UX14TargetSizeTests(TestCase):
    def test_touch_targets_24_mobile_44_delete_gap_8(self):
        css = CSS_PATH.read_text()
        for sel in (r"\.btn\s*\{([^}]*)\}", r"\.row-action\s*\{([^}]*)\}",
                    r"\.util-bar\s*\{([^}]*)\}"):
            m = re.search(sel, css)
            self.assertIsNotNone(m, sel)
            mh = re.search(r"min-height\s*:\s*(\d+)px", m.group(1))
            self.assertIsNotNone(mh, sel)
            self.assertGreaterEqual(int(mh.group(1)), 24, sel)
        for sel in (r"\.btn\s*\{([^}]*)\}", r"\.row-action\s*\{([^}]*)\}",
                    r"\.util-bar\s*\{([^}]*)\}"):
            m = re.search(sel, css)
            mh = re.search(r"min-height\s*:\s*(\d+)px", m.group(1))
            self.assertIsNotNone(mh, sel)
            self.assertGreaterEqual(int(mh.group(1)), 44, sel + " touch")
        self.assertRegex(css, r"@media\s*\(max-width:\s*700px\)")
        m = re.search(r"([^{}]*\.btn-row[^{}]*)\{([^}]*)\}", css)
        self.assertIsNotNone(m, "missing .btn-row rule")
        self.assertRegex(m.group(2), r"gap\s*:\s*8px")
        m2 = re.search(r"\.row-action\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m2, ".row-action")
        self.assertRegex(m2.group(1), r"margin\s*:")


class UX04HumanRowsTests(TestCase):
    def test_no_raw_tuples_human_sentences_how_calculated(self):
        _, f, c = owner_client()
        seed_overload(f)
        r = c.get(reverse("plans"))
        b = r.content.decode()
        self.assertNotIn("(2026,", b)
        self.assertTrue("Traktor" in b or "P1" in b)
        self.assertTrue("hétköznap" in b or "weekday" in b)
        rows = r.context["weather_summary"]["equipment_rows"]
        traktor = [x for x in rows if x["equipment_name"] == "Traktor"]
        self.assertTrue(traktor, rows)
        self.assertIsNotNone(traktor[0]["weekly_capacity"])
        cap = str(traktor[0]["weekly_capacity"]).replace(".", r"[,.]")
        self.assertIsNotNone(
            re.search(r"Traktor</td>\s*<td>[^<]*</td>\s*<td>\s*%s\s*</td>" % cap, b),
            "equipment row must show explicit name + capacity")

    def test_how_calculated_shared_on_both(self):
        _, f, c = owner_client()
        seed_overload(f)
        for url in (reverse("plans"), reverse("utilization")):
            b = c.get(url).content.decode()
            self.assertTrue("hétköznap" in b or "weekday" in b, url)


class UX28NeutralOptionsTests(TestCase):
    def test_two_neutral_options_both_pages_localized(self):
        _, f, c = owner_client()
        seed_overload(f)
        c.cookies["django_language"] = "hu"
        for url in (reverse("plans"), reverse("utilization")):
            b = c.get(url).content.decode()
            lis = re.findall(r"<li>([^<]{10,300})</li>", b)
            self.assertIn("áthelyezhet", " ".join(lis), "hu" + url)
        c.cookies["django_language"] = "en"
        for url in (reverse("plans"), reverse("utilization")):
            b = c.get(url).content.decode()
            lis = re.findall(r"<li>([^<]{10,300})</li>", b)
            self.assertIn("reschedul", " ".join(lis), "en" + url)


class CacheBustTests(TestCase):
    def test_new_css_version(self):
        _, _, c = owner_client()
        b = c.get(reverse("dashboard")).content.decode()
        self.assertIn("portal/app.css?" + CSS_VERSION, b)


class AllScrollRegionRegression(TestCase):
    def test_advice_request_profile_table_is_named_region(self):
        from django.contrib.auth import get_user_model
        from portal.models import Farm
        u = get_user_model().objects.create_user(email="scroll-regression@example.invalid", password="TestPassword123!")
        Farm.objects.create(owner=u, name="Synthetic")
        self.client.force_login(u)
        response = self.client.get("/tanacs/keres/")
        self.assertContains(response, 'class="table-scroll" tabindex="0" role="region" aria-label=')
