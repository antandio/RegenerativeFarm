"""Task8 RED: secure document library — real assertions, must FAIL before prod code."""
import hashlib
import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings

from portal.models import Document


class TempPrivateRootMixin:
    """Isolated storage override: every test gets a fresh temp
    PRIVATE_DOCUMENT_ROOT; the live dir is never touched and the temp
    dir is always removed. Subclasses must call super().setUp() first."""

    def setUp(self):
        super().setUp()
        self._old_root = settings.PRIVATE_DOCUMENT_ROOT
        self._tmp = tempfile.mkdtemp(prefix="task8docs-")
        settings.PRIVATE_DOCUMENT_ROOT = Path(self._tmp)

    def tearDown(self):
        settings.PRIVATE_DOCUMENT_ROOT = getattr(
            self, "_old_root", settings.PRIVATE_DOCUMENT_ROOT)
        tmp = getattr(self, "_tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        super().tearDown()


def make_user(email, password="Teszt-jelszo-123"):
    from portal.models import User
    u = User.objects.create_user(email=email, password=password)
    from portal.models import Farm
    Farm.objects.get_or_create(owner=u, defaults={"name": "Teszt gazdaság"})
    return u


def login(client, user, password="Teszt-jelszo-123"):
    assert client.login(username=user.email, password=password)


def pdf_bytes(payload=b"hello"):
    return b"%PDF-1.4\n%\xe2\xe3\n" + payload + b"\n%%EOF\n"


def docx_bytes(text="hello", extra_files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", f"<w:document>{text}</w:document>")
        for name, data in (extra_files or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def upload(client, name, data, ctype="application/octet-stream", doc_name="Doksi", public=False):
    from django.core.files.uploadedfile import SimpleUploadedFile
    f = SimpleUploadedFile(name, data, content_type=ctype)
    return client.post("/dokumentumok/uj/", {
        "name": doc_name, "description": "", "is_public": public, "upload": f,
    })


class DocumentModelTests(TestCase):
    def test_default_private_false(self):
        u = make_user("m1@example.com")
        d = Document(owner=u, name="N", original_filename="a.pdf",
                     storage_name="x.pdf", extension=".pdf",
                     content_type="application/pdf", size_bytes=10, sha256="0" * 64)
        self.assertFalse(d.is_public, "default must be private (False)")


class DocumentUploadTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.u = make_user("u1@example.com")
        self.c = Client()
        login(self.c, self.u)

    def test_valid_pdf_upload(self):
        data = pdf_bytes()
        r = upload(self.c, "spec.pdf", data, "application/pdf", "Spec")
        self.assertEqual(r.status_code, 302, f"valid pdf must redirect, got {r.status_code}")
        d = Document.objects.get(name="Spec")
        self.assertEqual(d.sha256, hashlib.sha256(data).hexdigest())
        self.assertTrue((settings.PRIVATE_DOCUMENT_ROOT / d.storage_name).exists())
        self.assertNotIn("spec", Document.objects.get().storage_name)
        self.assertNotIn("/", d.storage_name)

    def test_reject_exe_and_double_extension(self):
        for fname in ("evil.exe", "doc.pdf.exe", "page.html", "pic.svg"):
            r = upload(self.c, fname, b"MZ fake", "application/octet-stream", f"N-{fname}")
            self.assertEqual(r.status_code, 400, f"{fname} must be 400, got {r.status_code}")
        self.assertEqual(Document.objects.count(), 0)
        root = settings.PRIVATE_DOCUMENT_ROOT
        files = list(root.iterdir()) if root.exists() else []
        self.assertEqual(files, [], "no file may remain after rejected uploads")

    def test_reject_magic_spoof(self):
        r = upload(self.c, "fake.pdf", "plain text not pdf".encode(), "application/pdf", "Fake")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_reject_oversize(self):
        big = b"%PDF-" + b"0" * (10 * 1024 * 1024 + 1 - 5)
        r = upload(self.c, "big.pdf", big, "application/pdf", "Big")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_reject_nul_txt(self):
        r = upload(self.c, "a.txt", b"hello\x00world", "text/plain", "Nul")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_reject_bad_docx_missing_parts(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w/>")
        r = upload(self.c, "b.docx", buf.getvalue(),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Bad")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_reject_docx_traversal_and_macro(self):
        evil = docx_bytes(extra_files={"../../evil.txt": b"x", "word/vbaProject.bin": b"macro"})
        r = upload(self.c, "m.docx", evil,
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Macro")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_traversal_filename_sanitized(self):
        r = upload(self.c, "../../etc/passwd.pdf", pdf_bytes(), "application/pdf", "Trav")
        self.assertEqual(r.status_code, 302)
        d = Document.objects.get(name="Trav")
        self.assertNotIn("/", d.original_filename)
        self.assertNotIn("..", d.original_filename)
        self.assertNotIn("/", d.storage_name)


class DocumentAccessTests(TempPrivateRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("own@example.com")
        self.other = make_user("oth@example.com")
        self.co = Client()
        self.ct = Client()
        login(self.co, self.owner)
        login(self.ct, self.other)

    def _create(self, public=False, data=None, fname="d.pdf"):
        data = data or pdf_bytes()
        r = upload(self.co, fname, data, "application/pdf", "D", public=public)
        self.assertEqual(r.status_code, 302)
        return Document.objects.get(name="D"), data

    def test_private_download_idor_404_public_200(self):
        d, data = self._create(public=False)
        r = self.ct.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r.status_code, 404, "other user private download must be 404")
        d.is_public = True
        d.save()
        r2 = self.ct.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r2.status_code, 200)
        anon = Client()
        r3 = anon.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r3.status_code, 302, "anonymous must redirect to login")

    def test_download_headers_no_path_leak(self):
        d, data = self._create(public=True)
        r = self.ct.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r.status_code, 200)
        cd = r.get("Content-Disposition", "")
        self.assertIn("attachment", cd)
        self.assertEqual(r.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.get("Content-Type"), "application/pdf")
        self.assertIn("private", r.get("Cache-Control", ""))
        self.assertIn("no-store", r.get("Cache-Control", ""))
        body = b"".join(r.streaming_content) if hasattr(r, "streaming_content") else r.content
        self.assertEqual(body, data)
        html = self.ct.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertNotIn("private_documents", html)
        self.assertNotIn(d.storage_name, html)

    def test_public_edit_denied(self):
        d, _ = self._create(public=True)
        r = self.ct.post(f"/dokumentumok/{d.pk}/szerkesztes/",
                         {"name": "Hacked", "description": "", "is_public": True})
        self.assertEqual(r.status_code, 404)
        d.refresh_from_db()
        self.assertEqual(d.name, "D")

    def test_replacement_retention_on_invalid(self):
        d, old = self._create(public=False)
        old_path = settings.PRIVATE_DOCUMENT_ROOT / d.storage_name
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad = SimpleUploadedFile("evil.exe", b"MZ", content_type="application/octet-stream")
        r = self.co.post(f"/dokumentumok/{d.pk}/szerkesztes/",
                         {"name": "D", "description": "", "upload": bad})
        self.assertEqual(r.status_code, 400)
        d.refresh_from_db()
        self.assertEqual(d.sha256, hashlib.sha256(old).hexdigest())
        self.assertTrue(old_path.exists(), "original file must survive failed replace")

    def test_no_get_writes(self):
        n0 = Document.objects.count()
        self.co.get("/dokumentumok/uj/")
        self.assertEqual(Document.objects.count(), n0)
        d, _ = self._create(public=False)
        self.co.get(f"/dokumentumok/{d.pk}/torles/")
        self.assertTrue(Document.objects.filter(pk=d.pk).exists(), "GET delete must not delete")

    def test_bulk_guard(self):
        d, _ = self._create(public=False)
        with self.assertRaises(ValueError):
            Document.objects.update(is_public=True)
        with self.assertRaises(ValueError):
            Document.objects.bulk_update([d], ["is_public"])

    def test_delete_removes_file(self):
        d, _ = self._create(public=False)
        p = settings.PRIVATE_DOCUMENT_ROOT / d.storage_name
        self.assertTrue(p.exists())
        # Production deletes the file in transaction.on_commit (only after the
        # DB row is really gone). TestCase never commits, so execute the
        # deferred callback explicitly to exercise the real path.
        with self.captureOnCommitCallbacks(execute=True):
            r = self.co.post(f"/dokumentumok/{d.pk}/torles/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Document.objects.filter(pk=d.pk).exists())
        self.assertFalse(p.exists())

    def test_csrf_enforced_on_delete(self):
        d, _ = self._create(public=False)
        evil = Client(enforce_csrf_checks=True)
        evil.login(username=self.owner.email, password="Teszt-jelszo-123")
        r = evil.post(f"/dokumentumok/{d.pk}/torles/", {})
        self.assertEqual(r.status_code, 403, "missing CSRF token must be 403")
        self.assertTrue(Document.objects.filter(pk=d.pk).exists())


class DocumentStorageHardeningTests(TempPrivateRootMixin, TestCase):
    """Root handling is temp-root only: the live PRIVATE_DOCUMENT_ROOT is
    never created or touched; the root appears lazily via private_root()
    on the write path with mode 0700, and symlinks are refused."""

    def test_private_root_created_lazily_with_0700(self):
        import os
        import stat
        from portal import document_storage
        fresh = Path(self._tmp) / "fresh-root"
        self.assertFalse(fresh.exists(), "root must not exist before write path")
        settings.PRIVATE_DOCUMENT_ROOT = fresh
        root = document_storage.private_root()
        self.assertTrue(root.is_dir())
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)

    def test_private_root_refuses_symlink(self):
        from portal import document_storage
        target = Path(self._tmp) / "real"
        target.mkdir()
        link = Path(self._tmp) / "linkroot"
        link.symlink_to(target)
        settings.PRIVATE_DOCUMENT_ROOT = link
        with self.assertRaises(RuntimeError):
            document_storage.private_root()
        self.assertTrue(target.is_dir(), "symlink target must be untouched")

    def test_storage_path_refuses_escape(self):
        from portal import document_storage
        with self.assertRaises(ValueError):
            document_storage.storage_path("../escape.pdf")
        with self.assertRaises(ValueError):
            document_storage.storage_path("sub/dir.pdf")

    def test_replacement_db_failure_cleans_new_file_keeps_old(self):
        from unittest import mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        owner = make_user("dbfail@example.com")
        c = Client()
        login(c, owner)
        data = pdf_bytes(b"original")
        r = upload(c, "orig.pdf", data, "application/pdf", "D")
        self.assertEqual(r.status_code, 302)
        d = Document.objects.get(name="D")
        old_path = settings.PRIVATE_DOCUMENT_ROOT / d.storage_name
        before = {p.name for p in settings.PRIVATE_DOCUMENT_ROOT.iterdir()}
        good = SimpleUploadedFile("new.pdf", pdf_bytes(b"new"),
                                  content_type="application/pdf")
        with mock.patch("portal.document_views.Document.save",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                c.post(f"/dokumentumok/{d.pk}/szerkesztes/",
                       {"name": "D", "description": "", "upload": good})
        after = {p.name for p in settings.PRIVATE_DOCUMENT_ROOT.iterdir()}
        self.assertEqual(before, after, "failed replace must leave no new file behind")
        self.assertTrue(old_path.exists(), "original file must survive failed replace")
        d.refresh_from_db()
        self.assertEqual(d.sha256, hashlib.sha256(data).hexdigest())


class DocumentReviewFixTests(TempPrivateRootMixin, TestCase):
    """Task8 review MF1-4 regressions: RED before fix, GREEN after."""

    def setUp(self):
        super().setUp()
        self.u = make_user("rev8@example.com")
        self.c = Client()
        login(self.c, self.u)

    def _store_doc(self, owner, name="R", data=None, original="r.pdf"):
        from portal.document_storage import new_storage_name, write_new_file
        data = data or pdf_bytes()
        sname = new_storage_name(".pdf")
        write_new_file(sname, data)
        d = Document.objects.create(
            owner=owner, name=name, description="", original_filename=original,
            storage_name=sname, extension=".pdf", content_type="application/pdf",
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        return d, data

    def test_mf1_owner_delete_cascades_file_cleanup(self):
        from portal.models import User
        isolated = User.objects.create_user(
            email="iso8@example.com", password="Teszt-jelszo-123")
        d, _ = self._store_doc(isolated, name="Iso")
        p = settings.PRIVATE_DOCUMENT_ROOT / d.storage_name
        self.assertTrue(p.exists())
        with self.captureOnCommitCallbacks(execute=True):
            isolated.delete()
        self.assertFalse(Document.objects.filter(pk=d.pk).exists())
        self.assertFalse(p.exists(), "owner delete must remove private bytes")

    def test_mf1_queryset_delete_removes_files(self):
        d1, _ = self._store_doc(self.u, name="Q1")
        d2, _ = self._store_doc(self.u, name="Q2")
        p1 = settings.PRIVATE_DOCUMENT_ROOT / d1.storage_name
        p2 = settings.PRIVATE_DOCUMENT_ROOT / d2.storage_name
        with self.captureOnCommitCallbacks(execute=True):
            Document.objects.filter(owner=self.u).delete()
        self.assertFalse(Document.objects.filter(owner=self.u).exists())
        self.assertFalse(p1.exists())
        self.assertFalse(p2.exists())

    def test_mf1_delete_rollback_retains_file(self):
        from django.db import transaction
        d, _ = self._store_doc(self.u, name="RB")
        p = settings.PRIVATE_DOCUMENT_ROOT / d.storage_name
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    Document.objects.filter(pk=d.pk).delete()
                    raise RuntimeError("rollback")
        self.assertTrue(Document.objects.filter(pk=d.pk).exists())
        self.assertTrue(p.exists(), "rollback must retain the file")

    def test_mf2_content_disposition_injection_safe(self):
        import re
        import urllib.parse
        evil = "x\"; filename*=UTF-8''pwned.pdf"
        d, _ = self._store_doc(self.u, name="H", original=evil)
        r = self.c.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r.status_code, 200)
        cd = r.get("Content-Disposition", "")
        m = re.fullmatch(
            r"attachment; filename=\"([A-Za-z0-9._-]+\.pdf)\"; filename\*=UTF-8''(\S+)",
            cd)
        self.assertIsNotNone(m, f"unsafe Content-Disposition: {cd!r}")
        self.assertEqual(urllib.parse.unquote(m.group(2)), evil)

    def test_mf2_non_ascii_filename_roundtrip(self):
        import re
        import urllib.parse
        original = "Téli jelentés.pdf"
        d, _ = self._store_doc(self.u, name="N", original=original)
        r = self.c.get(f"/dokumentumok/{d.pk}/letoltes/")
        self.assertEqual(r.status_code, 200)
        cd = r.get("Content-Disposition", "")
        m = re.fullmatch(
            r"attachment; filename=\"([A-Za-z0-9._-]+\.pdf)\"; filename\*=UTF-8''(\S+)",
            cd)
        self.assertIsNotNone(m, f"unsafe Content-Disposition: {cd!r}")
        self.assertEqual(urllib.parse.unquote(m.group(2)), original)

    def test_mf3_reject_late_nul_txt(self):
        data = b"a" * 9000 + b"\x00" + b"b" * 100
        r = upload(self.c, "late.txt", data, "text/plain", "Late")
        self.assertEqual(r.status_code, 400, "NUL past 8 KiB must be rejected")
        self.assertEqual(Document.objects.count(), 0)

    def test_mf4_uploaded_file_has_0600(self):
        import os
        import stat
        data = pdf_bytes()
        r = upload(self.c, "m.pdf", data, "application/pdf", "M")
        self.assertEqual(r.status_code, 302)
        d = Document.objects.get(name="M")
        mode = stat.S_IMODE(os.stat(
            settings.PRIVATE_DOCUMENT_ROOT / d.storage_name).st_mode)
        self.assertEqual(mode, 0o600)

    def test_mf4_preexisting_root_wrong_mode_is_fixed(self):
        import os
        import stat
        from portal import document_storage
        root = Path(self._tmp) / "old-root"
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
        settings.PRIVATE_DOCUMENT_ROOT = root
        fixed = document_storage.private_root()
        self.assertEqual(stat.S_IMODE(os.stat(fixed).st_mode), 0o700)

    def test_mf4_chmod_failure_no_db_or_file(self):
        from unittest import mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        from portal import document_storage
        root = document_storage.private_root()
        data = pdf_bytes()
        with mock.patch("os.chmod", side_effect=[None, OSError("denied")]):
            with self.assertRaises(OSError):
                self.c.post("/dokumentumok/uj/", {
                    "name": "F", "description": "",
                    "upload": SimpleUploadedFile(
                        "f.pdf", data, content_type="application/pdf")})
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(list(root.iterdir()), [],
                         "failed hardening must leave no file behind")
