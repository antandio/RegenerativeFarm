"""Task8 persistent i18n: HU/EN coverage for the document library.

Covers model/storage/form/views/templates/sidebar strings: EN create/detail
labels, privacy badges, validation errors (rendered pages) plus direct msgid
translation assertions. Fails while catalogs lack the new entries (RED before
makemessages), passes after HU/EN catalogs are filled and compiled.
"""
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.utils.translation import override, gettext


EN_MSGIDS = {
    "Dokumentumtár": "Document library",
    "Új dokumentum": "New document",
    "Dokumentum neve": "Document name",
    "Fájl": "File",
    "Nyilvános mások számára": "Public for others",
    "Rövid leírás a dokumentumról.": "Short description of the document.",
    "A fájl kötelező.": "A file is required.",
    "Érvénytelen dokumentumadatok.": "Invalid document data.",
    "Dokumentum feltöltve.": "Document uploaded.",
    "Dokumentum frissítve.": "Document updated.",
    "Dokumentum törölve.": "Document deleted.",
    "Megosztott": "Shared",
    "Még nincs dokumentum.": "No documents yet.",
    "bájt": "bytes",
    "Eredeti fájlnév:": "Original filename:",
    "Letöltés": "Download",
    "Nem támogatott fájltípus. Engedélyezett: PDF, TXT, MD, CSV, DOCX.":
        "Unsupported file type. Allowed: PDF, TXT, MD, CSV, DOCX.",
    "Üres fájl nem tölthető fel.": "Empty files cannot be uploaded.",
    "A fájl túl nagy (legfeljebb 10 MiB).": "The file is too large (max 10 MiB).",
    "Érvénytelen PDF-aláírás.": "Invalid PDF signature.",
    "A szöveges fájl nem tartalmazhat NUL-bájtot.":
        "Text files must not contain NUL bytes.",
    "A szöveges fájlnak UTF-8 kódolásúnak kell lennie.":
        "Text files must be UTF-8 encoded.",
    "Érvénytelen DOCX-aláírás.": "Invalid DOCX signature.",
    "Belső tárolási hiba.": "Internal storage error.",
    "Biztosan törlöd a dokumentumot? A művelet nem vonható vissza.":
        "Really delete this document? This cannot be undone.",
}


def switch(client, lang, next_url="/dokumentumok/"):
    return client.post("/i18n/setlang/", {"language": lang, "next": next_url})


def make_user(email, password="Teszt-jelszo-123"):
    from portal.models import Farm, User
    u = User.objects.create_user(email=email, password=password)
    Farm.objects.get_or_create(owner=u, defaults={"name": "Teszt gazdaság"})
    return u


def login(client, user, password="Teszt-jelszo-123"):
    assert client.login(username=user.email, password=password)


def pdf_bytes(payload=b"hello"):
    return b"%PDF-1.4\n%\xe2\xe3\n" + payload + b"\n%%EOF\n"


class DocumentI18nMsgidTests(TestCase):
    def test_en_msgids_have_english_translations(self):
        missing = []
        with override("en"):
            for msgid, expected in EN_MSGIDS.items():
                got = gettext(msgid)
                if got != expected:
                    missing.append(f"{msgid!r}: got {got!r}, want {expected!r}")
        self.assertEqual(missing, [], "missing/wrong EN catalog entries:\n" + "\n".join(missing))

    def test_hu_msgids_stay_hungarian(self):
        with override("hu"):
            for msgid in EN_MSGIDS:
                self.assertEqual(gettext(msgid), msgid)


class DocumentI18nRenderedTests(TestCase):
    def setUp(self):
        self._old_root = settings.PRIVATE_DOCUMENT_ROOT
        self._tmp = tempfile.mkdtemp(prefix="task8i18n-")
        settings.PRIVATE_DOCUMENT_ROOT = Path(self._tmp)
        from portal.models import User  # noqa: F401
        self.u = make_user("i18n8@example.com")
        login(self.client, self.u)

    def tearDown(self):
        settings.PRIVATE_DOCUMENT_ROOT = self._old_root
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    def test_en_create_labels_no_hungarian(self):
        switch(self.client, "en")
        b = self.client.get("/dokumentumok/uj/").content.decode()
        self.assertIn('<html lang="en">', b)
        for en in ("New document", "Document name", "Public for others",
                   "File", "Save", "Cancel"):
            self.assertIn(en, b, f"{en} missing on EN create page")
        for hu in ("Új dokumentum", "Dokumentum neve", "Nyilvános mások számára",
                   "Mentés", "Mégse"):
            self.assertNotIn(hu, b, f"{hu} leaked on EN create page")

    def test_en_detail_privacy_labels(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("spec.pdf", pdf_bytes(), content_type="application/pdf")
        r = self.client.post("/dokumentumok/uj/",
                             {"name": "Spec", "description": "",
                              "is_public": False, "upload": f})
        self.assertEqual(r.status_code, 302)
        from portal.models import Document
        d = Document.objects.get(name="Spec")
        switch(self.client, "en")
        b = self.client.get(f"/dokumentumok/{d.pk}/").content.decode()
        self.assertIn('<html lang="en">', b)
        for en in ("Private", "Download", "Original filename:",
                   "Stored file:", "bytes", "Updated:"):
            self.assertIn(en, b, f"{en} missing on EN detail page")
        for hu in ("Privát", "Letöltés", "Eredeti fájlnév:",
                   "Tárolt fájl:", "bájt", "Frissítve:"):
            self.assertNotIn(hu, b, f"{hu} leaked on EN detail page")

    def test_en_validation_errors_english(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        switch(self.client, "en")
        bad = SimpleUploadedFile("evil.exe", b"MZ fake",
                                 content_type="application/octet-stream")
        r = self.client.post("/dokumentumok/uj/",
                             {"name": "Bad", "description": "",
                              "is_public": False, "upload": bad})
        self.assertEqual(r.status_code, 400)
        b = r.content.decode()
        self.assertIn("Unsupported file type.", b)
        self.assertNotIn("Nem támogatott fájltípus.", b)
