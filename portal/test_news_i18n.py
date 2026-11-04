"""Task10 i18n: HU/EN coverage for news-source collection.

RED before catalogs: EN list/create/detail render English with no HU
leakage; validation errors translated. Passes after HU/EN entries added.
"""
from django.test import TestCase
from django.utils.translation import override, gettext

from portal.models import Farm
from portal.tests import make_user

EN_MSGIDS = {
    "Hírforrások": "News sources",
    "Új hírforrás": "New news source",
    "Portál neve": "Portal name",
    "Portál URL-je": "Portal URL",
    "Jegyzet": "Note",
    "Aktív": "Active",
    "Inaktív": "Inactive",
    "Megnyitás": "Open",
    "Még nincs hírforrás.": "No news sources yet.",
    "Mentés": "Save",
    "Mégse": "Cancel",
    "Szerkesztés": "Edit",
    "Törlés": "Delete",
    "Érvénytelen hírforrás-adatok.": "Invalid news source data.",
    "Hírforrás felvéve.": "News source added.",
    "Hírforrás frissítve.": "News source updated.",
    "Hírforrás törölve.": "News source deleted.",
    "Hírforrás aktiválva.": "News source activated.",
    "Hírforrás inaktiválva.": "News source deactivated.",
    "Ilyen URL már szerepel a gyűjteményedben.": "This URL is already in your collection.",
    "Érvénytelen URL.": "Invalid URL.",
    "Csak http és https URL tárolható.": "Only http and https URLs can be stored.",
    "Érvénytelen URL: hiányzó hoszt.": "Invalid URL: missing host.",
    "Az URL nem tartalmazhat felhasználói adatot.": "The URL must not contain user info.",
    "Érvénytelen port az URL-ben.": "Invalid port in URL.",
    "Csak nyilvános című hoszt tárolható.": "Only a public host can be stored.",
    "Érvénytelen hosztnév az URL-ben.": "Invalid hostname in URL.",
    "Az URL nem tartalmazhat szóközt vagy vezérlőkaraktert.": "The URL must not contain spaces or control characters.",
    "Az URL legfeljebb 2000 karakter lehet.": "The URL can be at most 2000 characters.",
    "Csak nyilvános http(s) cím, például https://example.com/feed.": "Only a public http(s) address, e.g. https://example.com/feed.",
    "Rövid jegyzet a portálról.": "Short note about the portal.",
    "Kikapcsolva a portál a gyűjteményben marad, de inaktív.": "When off, the portal stays in the collection but is inactive.",
    "Add meg a portál nevét és nyilvános http(s) címét. A gyűjtemény későbbi automatikus feldolgozásra szolgál; most nincs ütemezett pásztázás.": "Enter the portal name and its public http(s) address. The collection is for later automatic processing; there is no scheduled scanning now.",
    "Saját hírportál-gyűjteményed. Ez a gyűjtemény későbbi automatikus feldolgozásra szolgál: most nincs ütemezett pásztázás, hálózati lekérés vagy ajánlatkészítés, a jövőbeli napi feldolgozás jelenleg nem fut.": "Your own news-portal collection. This collection is for later automatic processing: there is no scheduled scanning, network fetching or recommendation making now; the future daily processing is not running.",
    "Tárolt gyűjtemény: a rendszer most nem pásztázza a portált, nem kér le hálózati tartalmat, és nem készít ajánlatot.": "Stored collection: the system does not scan the portal now, fetches no network content, and makes no recommendations.",
    "Biztosan törlöd a hírforrást? A művelet nem vonható vissza.": "Really delete this news source? This cannot be undone.",
    "A jegyzet legfeljebb 2000 karakter.": "Notes can be at most 2000 characters.",
    "Hírforrás szerkesztése — %(name)s": "Edit news source — %(name)s",
    "Hírforrás törlése — %(name)s": "Delete news source — %(name)s",
}


def switch(client, lang, next_url="/hirforrasok/"):
    return client.post("/i18n/setlang/", {"language": lang, "next": next_url})


class NewsI18nMsgidTests(TestCase):
    def test_en_msgids_have_english_translations(self):
        missing = []
        with override("en"):
            for msgid, expected in EN_MSGIDS.items():
                got = gettext(msgid)
                if got != expected:
                    missing.append(f"{msgid!r}: got {got!r}, want {expected!r}")
        self.assertEqual(missing, [], "missing/wrong EN entries:\n" + "\n".join(missing))

    def test_hu_msgids_stay_hungarian(self):
        with override("hu"):
            for msgid in EN_MSGIDS:
                self.assertEqual(gettext(msgid), msgid)


class NewsI18nRenderedTests(TestCase):
    def setUp(self):
        self.u = make_user("i18n10@example.com")
        Farm.objects.get_or_create(owner=self.u, defaults={"name": "Teszt gazdaság"})
        self.client.force_login(self.u)

    def test_en_list_no_hungarian(self):
        switch(self.client, "en")
        b = self.client.get("/hirforrasok/").content.decode()
        self.assertIn('<html lang="en">', b)
        for en in ("News sources", "New news source", "No news sources yet."):
            self.assertIn(en, b, f"{en} missing on EN list")
        for hu in ("Hírforrások", "Új hírforrás", "Még nincs hírforrás."):
            self.assertNotIn(hu, b, f"{hu} leaked on EN list")

    def test_en_create_labels_no_hungarian(self):
        switch(self.client, "en")
        b = self.client.get("/hirforrasok/uj/").content.decode()
        for en in ("New news source", "Portal name", "Portal URL", "Save", "Cancel"):
            self.assertIn(en, b, f"{en} missing on EN create")
        for hu in ("Új hírforrás", "Portál neve", "Portál URL-je"):
            self.assertNotIn(hu, b, f"{hu} leaked on EN create")

    def test_en_detail_and_validation_no_hungarian(self):
        from portal.models import NewsSource
        o = NewsSource.objects.create(owner=self.u, name="Portál", url="https://example.com/feed")
        switch(self.client, "en")
        b = self.client.get(f"/hirforrasok/{o.pk}/").content.decode()
        self.assertIn("Stored collection:", b)
        self.assertNotIn("Tárolt gyűjtemény", b)
        r = self.client.post("/hirforrasok/uj/", {"name": "X", "url": "javascript:alert(1)", "notes": "", "is_active": "on"})
        self.assertEqual(r.status_code, 400)
        b = r.content.decode()
        self.assertIn("Invalid news source data.", b)
        self.assertNotIn("Érvénytelen hírforrás-adatok.", b)
