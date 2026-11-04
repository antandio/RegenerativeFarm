"""Task3 gaps: valid-name symlink/FIFO/root security, caps, i18n, CSS."""
import hashlib
import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, TestCase

from portal.models import Document
from portal.test_documents import TempPrivateRootMixin, login, make_user
from portal.test_markdown_preview import _section, _store


def _valid(ext=".md"):
    import uuid
    return f"{uuid.uuid4().hex}{ext}"


class PreviewGapTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("gap@example.com")
        self.other = make_user("gapoth@example.com")
        self.co = Client()
        self.ct = Client()
        login(self.co, self.owner)
        login(self.ct, self.other)

    def _md_doc(self, data=b"# Hi\n"):
        d = _store(self.owner, "G", data, "n.md", ".md", "text/markdown")
        return d

    def test_valid_hex_symlink_inside_rejected(self):
        target = Path(self._tmp) / "real.md"
        target.write_bytes(b"# outside\n")
        sname = _valid()
        (settings.PRIVATE_DOCUMENT_ROOT / sname).symlink_to(target)
        d = self._md_doc()
        d.storage_name = sname
        d.save()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("outside", r.content.decode(errors="replace"))

    def test_valid_hex_symlink_outside_dir_rejected(self):
        reg = settings.PRIVATE_DOCUMENT_ROOT / "regdir"
        reg.mkdir(exist_ok=True)
        outside = Path(self._tmp) / "outdir"
        outside.mkdir(exist_ok=True)
        (outside / "t.md").write_bytes(b"# evil\n")
        sname = _valid()
        (settings.PRIVATE_DOCUMENT_ROOT / sname).symlink_to(outside / "t.md")
        d = self._md_doc()
        d.storage_name = sname
        d.save()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("evil", r.content.decode(errors="replace"))

    def test_valid_hex_fifo_no_hang(self):
        sname = _valid()
        p = settings.PRIVATE_DOCUMENT_ROOT / sname
        try:
            os.mkfifo(p)
        except OSError:
            self.skipTest("fifo unsupported")
        d = self._md_doc()
        d.storage_name = sname
        d.save()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("nem", r.content.decode(errors="replace").lower())

    def test_valid_hex_directory_rejected(self):
        sname = _valid()
        (settings.PRIVATE_DOCUMENT_ROOT / sname).mkdir()
        d = self._md_doc()
        d.storage_name = sname
        d.save()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        self.assertEqual(r.status_code, 200)

    def test_root_symlink_unavailable(self):
        from portal import markdown_preview
        real = Path(self._tmp) / "realroot"
        real.mkdir()
        link = Path(self._tmp) / "linkroot2"
        if link.exists() or link.is_symlink():
            import shutil
            if link.is_symlink():
                link.unlink()
            else:
                shutil.rmtree(link, ignore_errors=True)
        link.symlink_to(real)
        old = settings.PRIVATE_DOCUMENT_ROOT
        settings.PRIVATE_DOCUMENT_ROOT = link
        try:
            res = markdown_preview.load_markdown_preview(_valid())
            self.assertFalse(res["available"])
        finally:
            settings.PRIVATE_DOCUMENT_ROOT = old

    def test_cap_read_counts_and_truncation(self):
        marker = b"ENDMARKER-UNIQUE-9f3c1a"
        big = b"# Big\n\n" + b"y " * (300 * 1024) + marker
        d = self._md_doc(data=big)
        real_read = os.read
        requested = []
        with mock.patch("os.read", wraps=real_read) as m:
            from portal import markdown_preview
            res = markdown_preview.load_markdown_preview(d.storage_name)
            for c in m.call_args_list:
                requested.append(c.args[1] if len(c.args) > 1 else c.kwargs.get("n", 0))
        self.assertTrue(res["truncated"])
        self.assertTrue(res["available"])
        self.assertNotIn(marker.decode(), res["html"])
        self.assertLessEqual(sum(requested), 256 * 1024 + 65536 + 1)

    def test_utf8sig_and_invalid_bytes(self):
        from portal import markdown_preview
        data = "# Cím\n\nhello \xff\xfe world\n".encode("utf-8", "ignore") + b"\xff\xfe"
        raw = b"\xef\xbb\xbf" + data
        d = self._md_doc(data=raw)
        res = markdown_preview.load_markdown_preview(d.storage_name)
        self.assertTrue(res["available"])
        self.assertIn("<h1", res["html"])

    def test_unauthorized_reader_not_called(self):
        d = self._md_doc()
        with mock.patch("portal.markdown_preview.load_markdown_preview") as m:
            r = self.ct.get(f"/dokumentumok/{d.pk}/")
            self.assertEqual(r.status_code, 404)
            m.assert_not_called()

    def test_cache_control_private_nostore(self):
        d = self._md_doc()
        r = self.co.get(f"/dokumentumok/{d.pk}/")
        cc = r.get("Cache-Control", "")
        self.assertIn("private", cc)
        self.assertIn("no-store", cc)

    def test_en_locale_labels_no_hu(self):
        d = self._md_doc()
        self.co.cookies.load({"django_language": "en"})
        html = self.co.get(f"/dokumentumok/{d.pk}/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
        sec = _section(html)
        self.assertIn("Preview", sec)
        self.assertNotIn("Előnézet", sec)

    def test_empty_unavailable_truncated_en(self):
        d = self._md_doc(data=b"")
        r = self.co.get(f"/dokumentumok/{d.pk}/", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode(errors="replace")
        self.assertIn("Preview", body)

    def test_css_and_region(self):
        d = self._md_doc(data=("# T\n\n" + "a" * 200 + "\n\n| a | b |\n|---|---|\n| 1 | 2 |\n").encode())
        html = self.co.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertIn("overflow-wrap:anywhere", html)
        self.assertIn('role="region"', html)
        self.assertIn('tabindex="0"', html)
