"""Task2 RED: compact modern journal search (fails before implementation)."""
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from portal.models import Farm

User = get_user_model()
PW = "TesztJelszo123!"


def owner_client(email="task2-owner@example.com"):
    u = User.objects.create_user(email=email, password=PW)
    f = Farm.objects.create(owner=u, name="T2 Farm")
    c = Client()
    c.force_login(u)
    return u, f, c


class Task2CompactSearchTests(TestCase):
    def test_filter_form_compact_accessible(self):
        _, _, c = owner_client()
        html = c.get("/events/").content.decode()
        self.assertIn("journal-search", html)
        for name in ["date_from", "date_to", "property", "type"]:
            self.assertIn(f'for="id_{name}"', html)
            self.assertIn(f'id="id_{name}"', html)
        self.assertIn('method="get"', html.lower())
        i = html.find("journal-search")
        form_start = html.rfind("<form", 0, i)
        form_end = html.find("</form>", i)
        self.assertNotEqual(form_start, -1)
        self.assertNotEqual(form_end, -1)
        self.assertNotIn("csrfmiddlewaretoken", html[form_start:form_end])
        self.assertIn('href="/events/"', html)

    def test_hu_en_filter_labels(self):
        _, _, c = owner_client()
        hu = c.get("/events/", HTTP_ACCEPT_LANGUAGE="hu").content.decode()
        for s in ["Dátumtól", "Dátumig", "Terület", "Művelettípus", "Szűrés"]:
            self.assertIn(s, hu)
        c.cookies["django_language"] = "en"
        en = c.get("/events/").content.decode()
        for s in ["From date", "To date", "Filter"]:
            self.assertIn(s, en)

    def test_journal_search_css_scoped(self):
        import pathlib, re
        css = (pathlib.Path(__file__).parent / "static/portal/app.css").read_text()
        m = re.search(r"\.journal-search\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing scoped .journal-search rule")
        body = m.group(1)
        self.assertIn("display:", body)
        self.assertIn("flex-wrap", css)
        self.assertIn(".journal-search:focus-within", css)
        self.assertIn("journal-search", css.split("@media")[1] if "@media" in css else "")
