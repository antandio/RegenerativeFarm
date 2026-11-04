"""Catalog %-format invariant: msgstr placeholders must match msgid.

Guards against catalog corruption like the livestock delete guard
``Nem törölhető (%s). Archiválja a csoportot.`` whose EN msgstr lost the
``%s`` (``TypeError: not enough arguments for format string`` on
``/livestock/<pk>/delete/``) and whose HU msgstr duplicated the entry.
Parses the source .po files directly so the check fails even when the
compiled .mo is stale.
"""
import os
import re

from django.test import SimpleTestCase

PO_FILES = (
    "locale/en/LC_MESSAGES/django.po",
    "locale/hu/LC_MESSAGES/django.po",
)

STRIP_ESCAPED = re.compile(r"%%")
PLACEHOLDER = re.compile(r"%(\([^)]+\))?[#0\- +]*(?:\d+)?(?:\.\d+)?[diuoxXeEfFgGcrs]")


def placeholders(s):
    return PLACEHOLDER.findall(STRIP_ESCAPED.sub("", s))


def parse_po(path):
    entries = []
    msgid = msgstr = None
    target = None
    is_format = False
    pending_format = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("#"):
                if "python-format" in line:
                    pending_format = True
                continue
            if line.startswith("msgid"):
                if msgid is not None:
                    entries.append((msgid, msgstr, is_format))
                msgid = _unquote(line[len("msgid"):])
                msgstr = None
                target = "msgid"
                is_format = pending_format
                pending_format = False
                continue
            if line.startswith("msgstr"):
                msgstr = _unquote(line[len("msgstr"):])
                target = "msgstr"
                continue
            if line.startswith('"') and target:
                if target == "msgid":
                    msgid += _unquote(line)
                else:
                    msgstr += _unquote(line)
                continue
            if line == "" and msgid is not None and msgstr is not None:
                entries.append((msgid, msgstr, is_format))
                msgid = msgstr = None
                target = None
                is_format = False
                pending_format = False
    if msgid is not None and msgstr is not None:
        entries.append((msgid, msgstr, is_format))
    return entries


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\n", "\n")
    return ""


class CatalogFormatInvariantTests(SimpleTestCase):
    def test_msgstr_placeholders_match_msgid(self):
        base = os.getcwd()
        mismatches = []
        for rel in PO_FILES:
            path = os.path.join(base, rel)
            for msgid, msgstr, is_format in parse_po(path):
                if not msgid or not msgstr:
                    continue
                want = placeholders(msgid)
                got = placeholders(msgstr)
                if is_format or want or got:
                    if sorted(want) != sorted(got):
                        mismatches.append(
                            f"{rel}: msgid={msgid!r} want={want} msgstr={msgstr!r} got={got}"
                        )
        self.assertEqual(mismatches, [], "catalog %-format placeholder mismatch:\n" + "\n".join(mismatches))
