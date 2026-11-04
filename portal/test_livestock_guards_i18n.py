"""Task4 guard catalog regression: EN must translate guard msgids.

RED before catalog fix (fallback returns HU msgid), GREEN after
makemessages + EN translations + compilemessages.
"""
from django.test import TestCase
from django.utils import translation


GUARD_MSGIDS = [
    # livestock_views._delete_block_reasons + delete/edit guards
    "esemény-előzmény tartozik hozzá",
    "más csoportból ide mozgatott állatok nyoma van",
    "tervezett vagy elvégzett állatmunka kapcsolódik hozzá",
    "élő állományt tart nyilván",
    "Nem törölhető (%s). Archiválja a csoportot.",
    "Archivált csoport nem szerkeszthető. Előbb állítsa vissza.",
    # domain_models.LivestockEvent.reverse_delete guards
    "Az esemény már vissza lett vonva.",
    "Csak a csoport legutóbbi eseménye vonható vissza.",
    "Az átmozgatás nem vonható vissza: a célcsoporton újabb esemény van.",
    "Archivált csoport eseménye nem vonható vissza.",
    "Az esemény elvégzett munkához kapcsolódik, ezért nem vonható vissza.",
]


class LivestockGuardCatalogTests(TestCase):
    def test_guard_msgids_translate_in_en(self):
        from django.utils.translation import gettext as _
        missing = []
        with translation.override("en"):
            for msgid in GUARD_MSGIDS:
                got = _(msgid)
                if got == msgid:
                    missing.append(msgid)
        self.assertEqual(missing, [], f"EN catalog missing guard translations: {missing}")
