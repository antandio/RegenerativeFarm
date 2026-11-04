"""Runtime HU catalog regression guard, no transient baseline files."""
from django.test import SimpleTestCase
from django.utils.translation import override, gettext

class CatalogRegressionTests(SimpleTestCase):
    def test_hungarian_fallbacks_preserved(self):
        with override("hu"):
            for source in ["Áttekintés – gazdaportál", "nincs rögzítve", "Gazdálkodási napló – gazdaportál", "Permetezés"]:
                self.assertEqual(gettext(source), source)
