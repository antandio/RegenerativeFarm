"""Private TOML runtime configuration (stdlib only, no Django, no logging).

Optional, NOT versioned file: ./portal-config.toml
(chmod 600, excluded from git). The repo only contains the versioned
portal-config.example.toml sample (it is never loaded).

Default Meta endpoint:
- https://dev.meta.ai/docs/quickstart -> https://api.meta.ai/v1, muse-spark-1.3
- https://dev.meta.ai/docs/models -> muse-wonder-1.0 (standard mode, API)
base_url is understood WITHOUT the "/chat/completions" suffix; the advice-
(portal/provider.py) and chat- (portal/chat.py) clients append it.

Precedence (deterministic):
  PORTAL_<EXISTING_ENV_NAME> explicit process env
  > explicit TOML value
  > legacy env / .env name
  > default.
Examples: PORTAL_META_API_KEY > non-empty meta.api_key > META_API_KEY > empty.
PORTAL_DJANGO_DEBUG > site.debug > DJANGO_DEBUG > False.

- Empty TOML api_key = placeholder -> legacy fallback, never sent
  over the transport. Explicit empty dedicated env (PORTAL_META_API_KEY="")
  BLOCKS the legacy fallback instead (deliberate switch-off).
- systemd EnvironmentFile=.env keeps working (legacy layer);
  a set TOML key wins over the systemd-propagated legacy value.
- The loader never modifies os.environ, never re-exports secrets,
  logs nothing.
- Legacy layer: old compatible parsing, no extra validation
  (existing behavior when the file is missing), except the LANGUAGE_CODE /
  TIME_ZONE new surface (previously ignored).
  TOML + dedicated layer: strict validation (fail closed).
- Injectable: base_dir / config_path / environ (legacy) /
  dedicated_environ (PORTAL_* snapshot BEFORE the .env load).
"""
from __future__ import annotations

import ipaddress
import math
import os
import stat
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

CONFIG_FILENAME = "portal-config.toml"
CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024


class ConfigError(Exception):
    """General configuration error: mentions only the field name, never the value."""


def default_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def default_config_path(base_dir=None) -> Path:
    base = Path(base_dir) if base_dir is not None else default_base_dir()
    return base / CONFIG_FILENAME


# (return_key, section, toml_key, legacy_env, kind, default, lo, hi)
_SPEC = (
    ("DJANGO_DEBUG", "site", "debug", "DJANGO_DEBUG", "debug", False, None, None),
    ("DJANGO_ALLOWED_HOSTS", "site", "allowed_hosts", "DJANGO_ALLOWED_HOSTS",
     "hosts", ("127.0.0.1", "localhost"), None, None),
    ("LANGUAGE_CODE", "site", "language_code", "LANGUAGE_CODE", "lang", "hu", None, None),
    ("TIME_ZONE", "site", "time_zone", "TIME_ZONE", "tz", "Europe/Budapest", None, None),
    ("META_API_BASE_URL", "meta", "base_url", "META_API_BASE_URL",
     "url", "https://api.meta.ai/v1", None, None),
    ("META_MODEL", "meta", "model", "META_MODEL", "model", "muse-spark-1.3", None, None),
    ("META_API_KEY", "meta", "api_key", "META_API_KEY", "key", "", None, None),
    ("ADVICE_MAX_PER_HOUR_PER_FARM", "limits", "advice_max_per_hour_per_farm",
     "ADVICE_MAX_PER_HOUR_PER_FARM", "int", 5, 1, 100000),
    ("ADVICE_MAX_PER_DAY_PER_FARM", "limits", "advice_max_per_day_per_farm",
     "ADVICE_MAX_PER_DAY_PER_FARM", "int", 20, 1, 100000),
    ("ADVICE_GLOBAL_MAX_PER_DAY", "limits", "advice_global_max_per_day",
     "ADVICE_GLOBAL_MAX_PER_DAY", "int", 200, 1, 100000),
    ("ADVICE_REQUEST_TIMEOUT_SECONDS", "limits", "advice_request_timeout_seconds",
     "ADVICE_REQUEST_TIMEOUT_SECONDS", "float", 120.0, 1.0, 300.0),
    ("ADVICE_PENDING_STALE_MINUTES", "limits", "advice_pending_stale_minutes",
     "ADVICE_PENDING_STALE_MINUTES", "int", 15, 1, 1440),
    ("ADVICE_MAX_INPUT_CHARS", "limits", "advice_max_input_chars",
     "ADVICE_MAX_INPUT_CHARS", "int", 8000, 100, 1000000),
    ("ADVICE_MAX_OUTPUT_TOKENS", "limits", "advice_max_output_tokens",
     "ADVICE_MAX_OUTPUT_TOKENS", "int", 6000, 1, 128000),
    ("CHAT_REQUEST_TIMEOUT_SECONDS", "limits", "chat_request_timeout_seconds",
     "CHAT_REQUEST_TIMEOUT_SECONDS", "float", 45.0, 1.0, 300.0),
    ("CHAT_MAX_OUTPUT_TOKENS", "limits", "chat_max_output_tokens",
     "CHAT_MAX_OUTPUT_TOKENS", "int", 6000, 1, 128000),
    ("CHAT_MAX_INPUT_CHARS", "limits", "chat_max_input_chars",
     "CHAT_MAX_INPUT_CHARS", "int", 15000, 100, 1000000),
    ("CHAT_MAX_MESSAGE_CHARS", "limits", "chat_max_message_chars",
     "CHAT_MAX_MESSAGE_CHARS", "int", 2000, 100, 1000000),
)

_ALLOWED_TOP = {"config_version", "site", "meta", "limits"}
_ALLOWED_KEYS = {
    "site": {"debug", "allowed_hosts", "language_code", "time_zone"},
    "meta": {"base_url", "model", "api_key"},
    "limits": {s[2] for s in _SPEC if s[1] == "limits"},
}


def _bad(field):
    raise ConfigError("invalid value for field '%s'" % field) from None


def _unknown():
    raise ConfigError("unknown config field") from None


def _req_str(raw, field):
    if not isinstance(raw, str):
        _bad(field)
    return raw


def _check_bool_toml(v, field):
    if type(v) is not bool:
        _bad(field)
    return v


def _parse_debug_env(raw, field):
    raw = _req_str(raw, field)
    return raw.strip().lower() in ("1", "true", "yes")


_DEBUG_TRUE = ("1", "true", "yes")
_DEBUG_FALSE = ("0", "false", "no")


def _parse_debug_dedicated(raw, field):
    raw = _req_str(raw, field)
    s = raw.strip().lower()
    if s in _DEBUG_TRUE:
        return True
    if s in _DEBUG_FALSE:
        return False
    _bad(field)


def _check_host_entry(h, field):
    if not isinstance(h, str):
        _bad(field)
    h = h.strip()
    if not h or len(h) > 253:
        _bad(field)
    if "*" in h or "://" in h or "/" in h or "@" in h:
        _bad(field)
    if any(c.isspace() for c in h):
        _bad(field)
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    for lab in h.split("."):
        if not lab or len(lab) > 63:
            _bad(field)
        if lab.startswith("-") or lab.endswith("-"):
            _bad(field)
        if any(not (c.isalnum() or c == "-") for c in lab):
            _bad(field)
    return h


def _check_hosts_strict(hosts, field):
    if isinstance(hosts, str) or not isinstance(hosts, list):
        _bad(field)
    if not hosts:
        _bad(field)
    return [_check_host_entry(h, field) for h in hosts]


def _parse_hosts_env(raw, field):
    raw = _req_str(raw, field)
    return [h.strip() for h in raw.split(",") if h.strip()]


def _check_lang(value, field):
    if not isinstance(value, str):
        _bad(field)
    s = value.strip().lower()
    if s not in ("hu", "en"):
        _bad(field)
    return s


def _check_tz(value, field):
    if not isinstance(value, str):
        _bad(field)
    s = value.strip()
    try:
        ZoneInfo(s)
    except Exception:
        _bad(field)
    return s


def _looks_like_numeric_ip(host):
    if host and all(c.isdigit() or c == "." for c in host):
        return True
    h = host.lower()
    return h.startswith("0x") or ".0x" in h


def _check_key_strict(raw, field):
    s = _req_str(raw, field)
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        _bad(field)
    return s


def _check_hostname_labels(host, field):
    if "*" in host or "_" in host or " " in host:
        _bad(field)
    labels = host.split(".")
    if len(labels) < 2:
        _bad(field)
    for lab in labels:
        if not lab or len(lab) > 63:
            _bad(field)
        if lab.startswith("-") or lab.endswith("-"):
            _bad(field)
        if any(not (c.isalnum() or c == "-") for c in lab):
            _bad(field)


def _check_base_url(value, field):
    if not isinstance(value, str):
        _bad(field)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        _bad(field)
    if any(c.isspace() for c in value):
        _bad(field)
    v = value.strip()
    try:
        parts = urlsplit(v)
    except Exception:
        _bad(field)
    if parts.scheme != "https":
        _bad(field)
    if parts.username or parts.password:
        _bad(field)
    if parts.query or parts.fragment:
        _bad(field)
    try:
        port = parts.port
    except ValueError:
        _bad(field)
    if port is not None and not 1 <= port <= 65535:
        _bad(field)
    host = parts.hostname
    if not host:
        _bad(field)
    if host == "localhost":
        _bad(field)
    if host.endswith(".localhost") or host.endswith(".local"):
        _bad(field)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _looks_like_numeric_ip(host):
            _bad(field)
        _check_hostname_labels(host, field)
    else:
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            _bad(field)
    if (parts.path or "").rstrip("/").endswith("/chat/completions"):
        _bad(field)
    return v.rstrip("/")


def _parse_base_url_env(raw, field):
    raw = _req_str(raw, field)
    return raw.strip().rstrip("/")


def _check_model(value, field):
    if not isinstance(value, str):
        _bad(field)
    s = value.strip()
    if not s or len(s) > 120:
        _bad(field)
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        _bad(field)
    return s


def _check_count(num, field, lo, hi):
    if not lo <= num <= hi:
        _bad(field)
    return num


def _check_toml_int(v, field, lo, hi):
    if type(v) is not int:
        _bad(field)
    return _check_count(v, field, lo, hi)


def _check_toml_float(v, field, lo, hi):
    if type(v) is bool or not isinstance(v, (int, float)):
        _bad(field)
    f = float(v)
    if not math.isfinite(f):
        _bad(field)
    return _check_count(f, field, lo, hi)


def _parse_env_int(raw, field, lo, hi):
    raw = _req_str(raw, field)
    try:
        num = int(raw.strip())
    except Exception:
        _bad(field)
    return _check_count(num, field, lo, hi)


def _parse_env_float(raw, field, lo, hi):
    raw = _req_str(raw, field)
    try:
        num = float(raw.strip())
    except Exception:
        _bad(field)
    return _check_count(num, field, lo, hi)


def _resolve_field(spec, tdata, dedicated, legacy):
    key, section, tkey, leg, kind, default, lo, hi = spec
    ded_name = "PORTAL_" + leg
    field = section + "." + tkey
    # 1) dedicated process env (presence counts, empty api_key blocks the fallback)
    if ded_name in dedicated:
        raw = dedicated[ded_name]
        if kind == "key":
            s = _check_key_strict(raw, field)
            return "" if s.strip() == "" else s
        if kind == "debug":
            return _parse_debug_dedicated(raw, field)
        if kind == "hosts":
            return _check_hosts_strict(_parse_hosts_env(raw, field), field)
        if kind == "lang":
            return _check_lang(raw, field)
        if kind == "tz":
            return _check_tz(raw, field)
        if kind == "url":
            return _check_base_url(raw, field)
        if kind == "model":
            return _check_model(raw, field)
        if kind == "int":
            return _parse_env_int(raw, field, lo, hi)
        return _parse_env_float(raw, field, lo, hi)
    # 2) explicit TOML value (empty api_key = placeholder -> legacy fallback)
    if tdata is not None and tkey in tdata:
        v = tdata[tkey]
        if kind == "key":
            if not isinstance(v, str):
                _bad(field)
            if v.strip() == "":
                pass
            else:
                return _check_key_strict(v, field)
        elif kind == "debug":
            return _check_bool_toml(v, field)
        elif kind == "hosts":
            return _check_hosts_strict(v, field)
        elif kind == "lang":
            return _check_lang(v, field)
        elif kind == "tz":
            return _check_tz(v, field)
        elif kind == "url":
            return _check_base_url(v, field)
        elif kind == "model":
            return _check_model(v, field)
        elif kind == "int":
            return _check_toml_int(v, field, lo, hi)
        else:
            return _check_toml_float(v, field, lo, hi)
    # 3) legacy env / .env (old compatible parsing, no extra validation)
    if leg in legacy:
        raw = legacy[leg]
        if kind == "key":
            return _req_str(raw, field)
        if kind == "debug":
            return _parse_debug_env(raw, field)
        if kind == "hosts":
            return _parse_hosts_env(raw, field)
        if kind == "lang":
            return _check_lang(raw, field)
        if kind == "tz":
            return _check_tz(raw, field)
        if kind == "url":
            return _parse_base_url_env(raw, field)
        if kind == "model":
            return _req_str(raw, field)
        if kind == "int":
            return _parse_env_int(raw, field, lo, hi)
        return _parse_env_float(raw, field, lo, hi)
    # 4) default
    if kind == "hosts":
        return list(default)
    return default


def _extract_sections(data):
    for k in data:
        if k not in _ALLOWED_TOP:
            _unknown()
    if "config_version" in data:
        v = data["config_version"]
        if type(v) is not int or v != CONFIG_VERSION:
            _bad("config_version")
    sections = {}
    for sec in ("site", "meta", "limits"):
        raw = data.get(sec)
        if raw is None:
            sections[sec] = None
            continue
        if not isinstance(raw, dict):
            _bad(sec)
        for k in raw:
            if k not in _ALLOWED_KEYS[sec]:
                _unknown()
        sections[sec] = raw
    return sections


def _read_toml_file(path: Path):
    if path.is_symlink():
        raise ConfigError("unreadable configuration file") from None
    try:
        st = path.stat()
    except OSError:
        raise ConfigError("unreadable configuration file") from None
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError("insecure configuration file permissions") from None
    try:
        if st.st_size > MAX_CONFIG_BYTES:
            raise ConfigError("invalid configuration file") from None
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("invalid configuration file") from None
    if not isinstance(data, dict):
        raise ConfigError("invalid configuration file") from None
    return data


def load_runtime_config(base_dir=None, config_path=None, environ=None,
                        dedicated_environ=None):
    """Resolved private runtime config (flat dict). Never touches os.environ."""
    legacy = os.environ if environ is None else environ
    dedicated = legacy if dedicated_environ is None else dedicated_environ
    path = Path(config_path) if config_path is not None else default_config_path(base_dir)
    if path.is_symlink():
        raise ConfigError("unreadable configuration file") from None
    if not path.exists():
        data = None
    else:
        data = _read_toml_file(path)
    sections = (_extract_sections(data) if data is not None
                else {"site": None, "meta": None, "limits": None})
    resolved = {}
    for spec in _SPEC:
        resolved[spec[0]] = _resolve_field(spec, sections[spec[1]], dedicated, legacy)
    resolved["config_found"] = data is not None
    resolved["config_path"] = str(path) if data is not None else None
    return resolved
