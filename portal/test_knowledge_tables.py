"""Task2 RED: knowledge-menu listings use semantic accessible tables.

RED-first: the five knowledge-menu list routes plus the readonly
regenerative guide must render modern tables (or metadata tables for
details) with caption/scope/focusable scroll regions. These tests FAIL
on the legacy <ul class="rule-list"> markup and pass after GREEN.
English comments only; UI strings stay HU/EN via existing catalogs.
"""
import hashlib

from django.test import Client, TestCase

from portal.tests import make_user
from portal.test_documents import TempPrivateRootMixin


def owner_client(email):
    from portal.models import Farm
    u = make_user(email)
    farm = Farm.objects.filter(owner=u).first()
    if farm is None:
        farm = Farm.objects.create(owner=u, name="Farm " + email)
    c = Client()
    c.force_login(u)
    return u, farm, c


def make_rule(owner, name, public=False):
    from portal.models import RuleCategory, RuleDefinition
    cat, _ = RuleCategory.objects.get_or_create(owner=owner, name="Kat")
    return RuleDefinition.objects.create(
        owner=owner, category=cat, name=name, body="Body " + name,
        is_public=public)


def make_doc(owner, name, public=False):
    from portal.document_storage import new_storage_name, write_new_file
    from portal.models import Document
    data = b"%PDF-1.4\n%\xe2\xe3\n" + name.encode() + b"\n%%EOF\n"
    sname = new_storage_name(".pdf")
    write_new_file(sname, data)
    return Document.objects.create(
        owner=owner, name=name, description="", original_filename="d.pdf",
        storage_name=sname, extension=".pdf", content_type="application/pdf",
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        is_public=public)


class KnowledgeTablesRedTests(TempPrivateRootMixin, TestCase):
    def assertSemanticTable(self, html):
        self.assertIn("<table", html)
        self.assertIn("<caption", html)
        self.assertIn('scope="col"', html)
        self.assertIn('tabindex="0"', html)
        self.assertIn('role="region"', html)

    def test_rule_list_is_semantic_table(self):
        u, _, c = owner_client("kt-rule@example.com")
        make_rule(u, "Sz1")
        b = c.get("/szabalyok/").content.decode()
        self.assertEqual(c.get("/szabalyok/").status_code, 200)
        self.assertSemanticTable(b)

    def test_farm_rule_list_both_sections_are_tables(self):
        u, farm, c = owner_client("kt-farmrule@example.com")
        make_rule(u, "FSz1", public=True)
        b = c.get("/farm-szabalyok/").content.decode()
        self.assertEqual(c.get("/farm-szabalyok/").status_code, 200)
        self.assertSemanticTable(b)
        # Active full body stays reachable (details or expandable region).
        self.assertIn("Akt\u00edv szab\u00e1lyok", b)
        self.assertIn("El\u00e9rhet\u0151 le\u00edr\u00e1sok", b)

    def test_document_list_is_semantic_table(self):
        u, _, c = owner_client("kt-doc@example.com")
        make_doc(u, "Doksi1")
        b = c.get("/dokumentumok/").content.decode()
        self.assertEqual(c.get("/dokumentumok/").status_code, 200)
        self.assertSemanticTable(b)

    def test_farm_knowledge_list_both_sections_are_tables(self):
        u, farm, c = owner_client("kt-know@example.com")
        make_doc(u, "KDoksi1", public=True)
        b = c.get("/tudasbazis/").content.decode()
        self.assertEqual(c.get("/tudasbazis/").status_code, 200)
        self.assertSemanticTable(b)
        self.assertIn("Akt\u00edv dokumentumok", b)
        self.assertIn("El\u00e9rhet\u0151 dokumentumok", b)

    def test_news_source_list_is_semantic_table(self):
        from portal.models import NewsSource
        u, _, c = owner_client("kt-news@example.com")
        NewsSource.objects.create(
            owner=u, name="Portal", url="https://example.com/feed")
        b = c.get("/hirforrasok/").content.decode()
        self.assertEqual(c.get("/hirforrasok/").status_code, 200)
        self.assertSemanticTable(b)

    def assertMetaTable(self, html):
        self.assertIn("<table", html)
        self.assertIn("<caption", html)
        self.assertIn('scope="row"', html)
        self.assertIn('tabindex="0"', html)
        self.assertIn('role="region"', html)

    def test_rule_detail_is_meta_table(self):
        u, _, c = owner_client("kt-ruledet@example.com")
        r = make_rule(u, "SzDet")
        b = c.get("/szabalyok/%d/" % r.pk).content.decode()
        self.assertEqual(c.get("/szabalyok/%d/" % r.pk).status_code, 200)
        self.assertMetaTable(b)
        self.assertIn("Body SzDet", b)

    def test_document_detail_is_meta_table(self):
        u, _, c = owner_client("kt-docdet@example.com")
        d = make_doc(u, "DDet")
        b = c.get("/dokumentumok/%d/" % d.pk).content.decode()
        self.assertEqual(c.get("/dokumentumok/%d/" % d.pk).status_code, 200)
        self.assertMetaTable(b)

    def test_news_source_detail_is_meta_table(self):
        from portal.models import NewsSource
        u, _, c = owner_client("kt-newsdet@example.com")
        s = NewsSource.objects.create(
            owner=u, name="PortalDet", url="https://example.com/det")
        b = c.get("/hirforrasok/%d/" % s.pk).content.decode()
        self.assertEqual(c.get("/hirforrasok/%d/" % s.pk).status_code, 200)
        self.assertMetaTable(b)
        self.assertIn("https://example.com/det", b)

    def test_guide_has_meta_table(self):
        u, _, c = owner_client("kt-guide@example.com")
        b = c.get("/regenerativ-elvek/").content.decode()
        self.assertEqual(
            c.get("/regenerativ-elvek/").status_code, 200)
        self.assertMetaTable(b)

    def test_private_root_isolated_and_cleaned(self):
        import shutil
        from pathlib import Path
        from django.conf import settings
        live = Path(str(self._old_root))
        tmp = Path(str(settings.PRIVATE_DOCUMENT_ROOT))
        self.assertEqual(tmp, Path(self._tmp))
        self.assertNotEqual(tmp.resolve(), live.resolve())
        u, _, _ = owner_client("kt-tmproot@example.com")
        d = make_doc(u, "TmpIso")
        self.assertTrue((tmp / d.storage_name).exists())
        live_existed = live.exists()
        live_before = {p.name for p in live.iterdir()} if live_existed else set()
        self.assertNotIn(d.storage_name, live_before)
        # Cleanup removes exactly the temp dir; the live root is untouched.
        shutil.rmtree(str(tmp))
        self.assertFalse(tmp.exists())
        if live_existed:
            self.assertEqual({p.name for p in live.iterdir()}, live_before)
        else:
            self.assertFalse(live.exists())
        tmp.mkdir(parents=True, exist_ok=True)

    def test_farm_knowledge_active_names_link_only_with_permission(self):
        from django.urls import reverse
        from portal.tests import make_user as _mk
        u, farm, c = owner_client("kt-actlink@example.com")
        own = make_doc(u, "OwnActive", public=False)
        other = _mk("kt-actlink-other@example.com")
        pub = make_doc(other, "PubActive", public=True)
        priv = make_doc(other, "PrivOther", public=False)
        self.assertEqual(
            c.post(reverse("farm_knowledge_add", kwargs={"pk": own.pk})).status_code, 302)
        self.assertEqual(
            c.post(reverse("farm_knowledge_add", kwargs={"pk": pub.pk})).status_code, 302)
        denied = c.post(reverse("farm_knowledge_add", kwargs={"pk": priv.pk})).status_code
        self.assertIn(denied, (403, 404))
        b = c.get(reverse("farm_knowledge_list")).content.decode()
        self.assertIn('href="%s"' % reverse("document_detail", kwargs={"pk": own.pk}), b)
        self.assertIn('href="%s"' % reverse("document_detail", kwargs={"pk": pub.pk}), b)
        self.assertNotIn(reverse("document_detail", kwargs={"pk": priv.pk}), b)
        self.assertNotIn("PrivOther", b)
