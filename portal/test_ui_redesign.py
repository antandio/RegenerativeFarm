"""UI redesign regression: grouped operate-console shell (RED first, then GREEN)."""
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from portal.models import Farm, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def owner_client(email="ui-redesign@example.com"):
    u = User.objects.create_user(email=email, password=PW)
    f = Farm.objects.create(owner=u, name="UI Farm")
    other = User.objects.create_user(email="ui-other@example.com", password=PW)
    other_farm = Farm.objects.create(owner=other, name="Other UI Farm")
    Membership.objects.create(farm=other_farm, user=u, role="manager", is_active=True)
    c = Client()
    c.force_login(u)
    return u, f, c


class UIRedesignShellTests(TestCase):
    def test_grouped_sidebar_single_instance_no_duplicate_bottomnav(self):
        _, _, c = owner_client()
        r = c.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("portal/app.css?v=20261104-knowledge-tables", b)
        self.assertNotIn("portal/style.css", b)
        self.assertIn('id="main-content"', b)
        self.assertIn("app-sidebar", b)
        self.assertEqual(b.count("<aside"), 1, b[:4000])
        self.assertIn("mobile-nav", b)
        self.assertIn("<details", b)
        self.assertEqual(b.count("<details class=\"mobile-nav\""), 1, b[:4000])
        self.assertTrue("Menü" in b or "Menu" in b, b[:3000])
        for label in ["Áttekintés", "Gazdaságom", "Munkaterv", "Csevegés", "Fiók"]:
            self.assertIn(label, b)
        self.assertNotIn("bottomnav", b)
        self.assertIn('aria-current="page"', b)
        self.assertIn("<summary", b)
        self.assertIn("Escape", b)
        self.assertIn("Magyar HU", b)
        self.assertIn("English EN", b)
        self.assertIn("select_active_farm", b)
        # single farm selector form
        self.assertEqual(b.count("select_active_farm"), 1, b[:6000])

    def test_mobile_nav_in_normal_flow_and_logout_post(self):
        _, _, c = owner_client()
        r = c.get("/attekintes/")
        b = r.content.decode()
        # logout via POST form with CSRF (preserve function)
        self.assertIn('action="/kilepes/"', b)
        self.assertIn("csrfmiddlewaretoken", b)

    def test_overview_prioritizes_operations_and_collapses_advice(self):
        _, _, c = owner_client()
        b = c.get('/attekintes/').content.decode()
        self.assertIn('class="advice-summary"', b)

    def test_sidebar_section_headers_distinct_from_links(self):
        import pathlib, re
        css = (pathlib.Path(__file__).parent / "static/portal/app.css").read_text()
        m = re.search(r"\.app-sidebar h2\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing .app-sidebar h2 rule")
        body = m.group(1)
        for prop in ["color: #fff", "background: #2d5940", "text-transform: uppercase",
                      "letter-spacing: 0.08em", "padding: 8px 10px", "border-radius: 6px",
                      "font-size: 0.78rem", "line-height: 1.5", "font-weight: 700"]:
            self.assertIn(prop, body, prop)
        self.assertEqual(len(re.findall(r"\.app-sidebar h2\s*\{", css)), 1,
                         "duplicate .app-sidebar h2 rule overrides the new style")
        sep = re.search(r"\.app-sidebar section:not\(:first-child\)\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(sep, "missing section separator rule")
        self.assertIn("border-top", sep.group(1))
        self.assertIn("rgba(255,255,255", sep.group(1).replace(" ", ""))
        self.assertIn("padding-top: 14px", sep.group(1))
        _, _, c = owner_client()
        b = c.get("/attekintes/").content.decode()
        self.assertIn("portal/app.css?v=20261104-knowledge-tables", b)

    def test_english_shell_discovers_every_module(self):
        from django.urls import reverse
        _, _, c = owner_client()
        c.cookies['django_language'] = 'en'
        b = c.get(reverse('dashboard')).content.decode()
        for text in ('Menu', 'Planning', 'Team and account', 'Skip to content'):
            self.assertIn(text, b)
        for name in ('dashboard','farm','location_list','property_list','equipment_list','cycle_list','plans','weather','actions','chat','advice_request','advice_history','members','account'):
            self.assertIn('href="' + reverse(name) + '"', b)
        self.assertEqual(b.count('aria-current="page"'), 1)
