"""Task3 RED: markdown preview on document_detail — must FAIL before fix."""
import hashlib
import os
import stat
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.utils.translation import override

from portal.models import Document
from portal.test_documents import TempPrivateRootMixin, login, make_user, upload


def _section(html):
    """Return only the markdown-preview section (chrome scripts excluded)."""
    start = html.find('id="markdown-preview"')
    assert start != -1, "preview section missing"
    end = html.find("</section>", start)
    return html[start:end if end != -1 else len(html)]


def _store(owner, name, data, original, ext, ctype):
    from portal.document_storage import new_storage_name, write_new_file
    sname = new_storage_name(ext)
    write_new_file(sname, data)
    return Document.objects.create(
        owner=owner, name=name, description="", original_filename=original,
        storage_name=sname, extension=ext, content_type=ctype,
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


class MarkdownPreviewRedTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("mdown@example.com")
        self.other = make_user("mdoth@example.com")
        self.co = Client()
        self.ct = Client()
        login(self.co, self.owner)
        login(self.ct, self.other)

    def _md(self, owner=None, data=None, original="note.md", ext=".md", name="MD"):
        data = data if data is not None else (
            b"# Cim\n\n**felkover** es tabla:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n`kod`\n")
        return _store(owner or self.owner, name, data, original, ext, "text/markdown")

    def test_detail_shows_markdown_preview_section(self):
        d = self._md()
        html = self.co.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertIn('id="markdown-preview"', html)
        self.assertIn("markdown-preview", html)
        self.assertIn("<h1", html)
        self.assertIn("<strong>", html)
        self.assertIn("<table", html)
        self.assertIn("<code", html)

    def test_markdown_extension_also_previews(self):
        d = self._md(original="note.markdown", ext=".markdown", name="MD2")
        html = self.co.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertIn('id="markdown-preview"', html)

    def test_public_other_sees_revoked_other_gets_404_without_leak(self):
        d = self._md()
        self.assertEqual(self.ct.get(f"/dokumentumok/{d.pk}/").status_code, 404)
        d.is_public = True
        d.save()
        r = self.ct.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="markdown-preview"', r.content.decode())
        d.is_public = False
        d.save()
        r2 = self.ct.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r2.status_code, 404)
        self.assertNotIn("felkover", r2.content.decode(errors="replace"))

    def test_unsafe_markup_stripped_no_remote_resources(self):
        evil = ("# T\n\n<script>alert(1)</script>\n\n"
                "<a href=\"javascript:alert(1)\">x</a> "
                "<b onmouseover=\"alert(1)\">y</b>\n\n![img](https://evil.example/p.png)\n")
        d = self._md(data=evil.encode(), name="Evil")
        html = self.co.get(f"/dokumentumok/{d.pk}/").content.decode()
        sec = _section(html).lower()
        self.assertNotIn("<script", sec)
        self.assertNotIn("onmouseover", sec)
        self.assertNotIn("javascript:", sec)
        self.assertNotIn("<img", sec)
        self.assertNotIn("evil.example", sec)

    def test_oversize_truncated_with_download_hint(self):
        big = b"# Big\n\n" + b"x " * (300 * 1024)
        d = self._md(data=big, name="Big")
        html = self.co.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertIn('id="markdown-preview"', html)
        self.assertIn("letoltes", html.lower() + "download")
        tail = hashlib.sha256(big).hexdigest()  # never rendered
        self.assertNotIn(tail, html)
        # Full content must not be inlined: a marker near the end is absent.
        self.assertLess(len(html), len(big))

    def test_bom_and_invalid_bytes_safe_no_500(self):
        data = b"\xef\xbb\xbf# BOM\n\nhello \xff\xfe world\n"
        d = self._md(data=data, name="Bom")
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="markdown-preview"', r.content.decode(errors="replace"))

    def test_missing_blob_unavailable_no_leak_no_500(self):
        d = self._md(name="Gone")
        (settings.PRIVATE_DOCUMENT_ROOT / d.storage_name).unlink()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode(errors="replace")
        self.assertNotIn(d.storage_name, body)
        self.assertNotIn("private_documents", body)

    def test_symlink_traversal_nonregular_rejected_no_500(self):
        outside = Path(self._tmp) / "outside.md"
        outside.write_bytes(b"# outside\n")
        link = settings.PRIVATE_DOCUMENT_ROOT / "link.md"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink unsupported")
        d = self._md(name="Link")
        d.storage_name = "link.md"
        d.save()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("outside", r.content.decode(errors="replace"))
        d.storage_name = "../escape.md"
        d.save()
        r2 = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r2.status_code, 200)
        sub = settings.PRIVATE_DOCUMENT_ROOT / "sub"
        sub.mkdir(exist_ok=True)
        d.storage_name = "sub"
        d.save()
        r3 = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r3.status_code, 200)

    def test_non_md_has_no_preview(self):
        from portal.document_storage import new_storage_name, write_new_file
        data = b"%PDF-1.4\n%\xe2\xe3\nhello\n%%EOF\n"
        sname = new_storage_name(".pdf")
        write_new_file(sname, data)
        d = Document.objects.create(
            owner=self.owner, name="P", description="", original_filename="a.pdf",
            storage_name=sname, extension=".pdf", content_type="application/pdf",
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        html = self.co.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertNotIn('id="markdown-preview"', html)

    def test_download_bytes_unchanged(self):
        data = "# T\n\nbody\n".encode()
        d = self._md(data=data, name="Dl")
        self.co.get(f"/dokumentumok/{d.pk}/")
        r = self.co.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r.status_code, 200)
        body = b"".join(r.streaming_content)
        self.assertEqual(body, data)
