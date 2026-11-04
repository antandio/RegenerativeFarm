"""Task10 syntactic URL validation for the own news-portal collection.

No network is used here: no DNS resolution, no fetch, no redirect
handling, no scheduler, no cloud/LLM. This only rejects obviously
bad or dangerous input at storage time; it is NOT SSRF protection by
itself. The future scheduled-scan worker (explicitly NOT implemented)
must revalidate resolved IPs on every fetch, handle redirects safely,
and defend against DNS rebinding. Stored URLs are never fetched,
embedded, or sent anywhere by this task's code.
"""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_URL_LENGTH = 2000
ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

_WS_OR_CONTROL = re.compile(r"[\s\x00-\x1f\x7f]")
_INT_PART = re.compile(r"(0[xX][0-9a-fA-F]+|0[0-7]+|0|[1-9][0-9]*)$")


def _invalid(message):
    # Plain (field-less) error: model.clean() re-keys it to {"url": ...}
    # and form field cleaners propagate it via add_error() unchanged.
    raise ValidationError(message)


def _parse_numeric_host(host):
    """Return True iff host parses as a numeric IPv4 form (inet_aton rules).

    Canonical dotted-decimal public IPv4 never reaches here (it parses via
    ipaddress first); anything numeric that reaches here is obfuscated
    (hex/octal/leading-zero/decimal-int) or invalid — both are rejected.
    Pure-letter words like ``cafe.beef`` do not parse and fall through to
    the hostname checks.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return False
    values = []
    for part in parts:
        if not part or not _INT_PART.match(part):
            return False
        if part[:2].lower() == "0x":
            values.append(int(part, 16))
        elif len(part) > 1 and part[0] == "0" and part.isdigit():
            values.append(int(part, 8))
        else:
            values.append(int(part, 10))
    if len(values) == 1:
        return 0 <= values[0] <= 0xFFFFFFFF
    if len(values) == 2:
        return values[0] <= 0xFF and values[1] <= 0xFFFFFF
    if len(values) == 3:
        return all(v <= 0xFF for v in values[:2]) and values[2] <= 0xFFFF
    return all(v <= 0xFF for v in values)


def normalize_news_url(raw):
    """Validate and canonicalize a news-source URL.

    Returns the canonical ``url`` string stored in the DB (fragment
    stripped, default port dropped, host lowercased/IDNA, empty path
    normalized to ``/``). Raises ``ValidationError`` (field ``url``) for
    anything unsafe or malformed.
    """
    if not isinstance(raw, str):
        _invalid(_("Érvénytelen URL."))
    if not raw or raw != raw.strip():
        _invalid(_("Érvénytelen URL."))
    if len(raw) > MAX_URL_LENGTH:
        _invalid(_("Az URL legfeljebb 2000 karakter lehet."))
    if _WS_OR_CONTROL.search(raw) or "\\" in raw:
        _invalid(_("Az URL nem tartalmazhat szóközt vagy vezérlőkaraktert."))
    # Fragment is display-only state: strip canonically (safe choice).
    head = raw.split("#", 1)[0]
    if not head:
        _invalid(_("Érvénytelen URL."))
    try:
        parts = urlsplit(head)
    except ValueError:
        _invalid(_("Érvénytelen URL."))
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        _invalid(_("Csak http és https URL tárolható."))
    netloc = parts.netloc
    if not netloc:
        _invalid(_("Érvénytelen URL: hiányzó hoszt."))
    if "@" in netloc:
        _invalid(_("Az URL nem tartalmazhat felhasználói adatot."))
    host = parts.hostname or ""
    if not host:
        _invalid(_("Érvénytelen URL: hiányzó hoszt."))
    host = host.rstrip(".")
    if not host:
        _invalid(_("Érvénytelen URL: hiányzó hoszt."))
    try:
        port = parts.port
    except ValueError:
        _invalid(_("Érvénytelen port az URL-ben."))
    if port is not None and not 1 <= port <= 65535:
        _invalid(_("Érvénytelen port az URL-ben."))
    # IP literal (v4/v6): only a currently-global, non-multicast address.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not (literal.is_global and not literal.is_multicast):
            _invalid(_("Csak nyilvános című hoszt tárolható."))
    else:
        if _parse_numeric_host(host):
            _invalid(_("Csak nyilvános című hoszt tárolható."))
        lowered = host.lower()
        if lowered == "localhost":
            _invalid(_("Csak nyilvános című hoszt tárolható."))
        if lowered.endswith(".local"):
            _invalid(_("Csak nyilvános című hoszt tárolható."))
        if "." not in lowered:
            _invalid(_("Csak nyilvános című hoszt tárolható."))
        if lowered.rsplit(".", 1)[-1].isdigit():
            _invalid(_("Érvénytelen hosztnév az URL-ben."))
        try:
            host = lowered.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            _invalid(_("Érvénytelen hosztnév az URL-ben."))
        if len(host) > 253:
            _invalid(_("Érvénytelen hosztnév az URL-ben."))
        for label in host.split("."):
            if not label or len(label) > 63:
                _invalid(_("Érvénytelen hosztnév az URL-ben."))
            if label.startswith("-") or label.endswith("-"):
                _invalid(_("Érvénytelen hosztnév az URL-ben."))
            if not re.fullmatch(r"[a-z0-9-]+", label):
                _invalid(_("Érvénytelen hosztnév az URL-ben."))
    if port is not None and DEFAULT_PORTS[scheme] == port:
        port = None
    display = host
    if ":" in host and not host.startswith("["):
        display = f"[{host}]"
    authority = display if port is None else f"{display}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, authority, path, parts.query, ""))
