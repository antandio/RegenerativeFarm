"""Agricultural-purpose gates: local screen plus independent model check.

The local keyword screen can only block or defer, never approve on its
own: every planning dispatch additionally needs an independent model
classification of "agri". A model "nonagri"/"uncertain" verdict blocks,
so keyword phrasing alone cannot smuggle a non-agricultural request in.
"""
import re

_AGRI_PATTERNS = [
    r"vet[ée]s", r"arat[áa]s", r"talaj", r"term[őo]", r"n[öo]v[ée]ny",
    r"burgonya", r"b[úu]za", r"kukorica", r"napraforg[óo]", r"sz[őo]l[őo]",
    r"gy[üu]m[öo]lcs", r"[áa]llatteny[ée]szt[ée]s", r"[áa]llat[áa]llom[áa]ny",
    r"legel", r"takarm[áa]ny", r"ist[áa]ll[óo]", r"tr[áa]gya",
    r"komposzt", r"mulcs", r"vet[ée]sforg[óo]", r"parcella", r"t[áa]bla",
    r"hekt[áa]r", r"gazd[áa]lkod[áa]s", r"gazdas[áa]g", r"farm",
    r"[öo]nt[öo]z[ée]s", r"csapad[ée]k", r"fagy", r"asz[áa]ly",
    r"k[áa]rtev[őo]", r"gyom", r"permetez[ée]s", r"metsz[ée]s",
    r"sz[üu]ret", r"kasza", r"sz[ée]na", r"sil[óo]", r"fej[ée]s",
    r"toj[áa]s", r"m[ée]h", r"m[ée]z", r"halast[óo]", r"erd[őo]",
    r"gyep", r"r[ée]t", r"sow", r"harvest", r"soil", r"crop",
    r"livestock", r"pasture", r"irrigat", r"orchard", r"vineyard",
    r"wheat", r"maize", r"compost", r"mulch", r"hectare", r"farm",
]
_NONAGRI_PATTERNS = [
    r"kaszin[óo]", r"casino", r"jackpot", r"lott[óo]", r"fogad[áa]s",
    r"szerencsej[áa]t[ée]k", r"hitelk[áa]rtya", r"kriptovaluta",
    r"crypto", r"t[őo]zsde", r"r[ée]szv[ée]ny", r"fegyver",
    r"robban[óo]", r"politikai kamp[áa]ny", r"szavaz",
    r"v[áa]laszt[áa]s", r"hack", r"felt[öo]r", r"jelsz[óo]",
    r"password",
]
_AGRI_RE = re.compile("|".join(_AGRI_PATTERNS), re.IGNORECASE)
_NONAGRI_RE = re.compile("|".join(_NONAGRI_PATTERNS), re.IGNORECASE)


def local_screen(text):
    """Local pre-screen: "agri", "uncertain" or "nonagri". Never final."""
    blob = str(text or "")
    if _NONAGRI_RE.search(blob):
        return "nonagri"
    if _AGRI_RE.search(blob):
        return "agri"
    return "uncertain"


def is_agricultural_request(text, client=None, run=None):
    """True only when the independent model gate also says "agri".

    A local "nonagri" verdict blocks immediately. Otherwise the model
    classifier is asked; anything but an explicit "agri" blocks. The
    classification call is ledgered when a run is supplied.
    """
    if local_screen(text) == "nonagri":
        return False
    if client is None:
        return False
    verdict = client.classify_agri(str(text or ""))
    decision = str(verdict or "uncertain").strip().lower()
    _ = run  # reserved for the future ledger hook; ledgering lives in run.py
    return decision == "agri"
