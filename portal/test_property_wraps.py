"""Task4 mobile visual regression: property headers must wrap WORDS, never per-character.

RED first: fails on shared .domain-table overflow-wrap:anywhere applied to thead.
GREEN needs: table-layout:auto, thead th word-wrap normal, min-width cols,
.domain-table--properties min-width 900, long header max-width 220.
Browser driver (deployed) asserts header height <= ~4 lines + first row visible.
"""
import pathlib
import re

from django.test import TestCase

CSS_PATH = pathlib.Path(__file__).parent / "static/portal/app.css"


class PropertyHeaderWrapTests(TestCase):
    def test_headers_wrap_words_never_characters(self):
        css = CSS_PATH.read_text()
        thead = re.search(
            r'\.domain-table\s+thead\s+th\[scope="col"\]\s*\{(.*?)\}', css, re.S)
        self.assertIsNotNone(thead, "missing .domain-table thead th rule")
        body = thead.group(1)
        self.assertRegex(body, r"overflow-wrap\s*:\s*normal")
        self.assertRegex(body, r"word-break\s*:\s*normal")
        self.assertRegex(body, r"white-space\s*:\s*normal")
        self.assertRegex(body, r"min-width\s*:\s*90px")
        # shared cell rule must NOT apply anywhere-wrap to thead headers
        shared = re.search(r"\.domain-table\s+th\s*,\s*\.domain-table\s+td"
                           r"\s*\{(.*?)\}", css, re.S)
        if shared is not None:
            self.assertNotRegex(shared.group(1), r"overflow-wrap\s*:\s*anywhere",
                                "thead must not inherit overflow-wrap:anywhere")

    def test_properties_table_has_wide_shell_and_est_header_cap(self):
        css = CSS_PATH.read_text()
        m = re.search(r"\.domain-table--properties\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing .domain-table--properties rule")
        self.assertRegex(m.group(1), r"min-width\s*:\s*900px")
        self.assertIn("table-layout", css)
        est = re.search(r"\.col-est\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(est, "missing .col-est rule")
        self.assertRegex(est.group(1), r"max-width\s*:\s*220px")

    def test_property_template_uses_wide_modifier(self):
        html = (pathlib.Path(__file__).parent
                / "templates/portal/domain_list.html").read_text()
        self.assertIn("domain-table--properties", html)
        self.assertIn("col-est", html)

    def test_location_shell_stays_640_and_coords_nowrap(self):
        css = CSS_PATH.read_text()
        m = re.search(r"\.domain-table\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, "missing .domain-table rule")
        self.assertRegex(m.group(1), r"min-width\s*:\s*640px")
        for sel in (r"\.domain-table\s+\.coord\s*\{(.*?)\}",
                    r"\.domain-table\s+\.tz\s*\{(.*?)\}"):
            mm = re.search(sel, css, re.S)
            self.assertIsNotNone(mm, f"missing rule {sel}")
            self.assertRegex(mm.group(1), r"white-space\s*:\s*nowrap")
