"""Privat TOML futasideju konfiguracio tesztjei (vertical TDD).

Mockolt/injektallt env + tmp TOML, elo halozat es fizetos hivas nelkul.
Nem olvas .env-t, hitelesito fajlt, kulcsot; csak dummy ertekek.
"""
import importlib
import json
import logging
import os

import httpx
from django.test import SimpleTestCase

from config.runtime import ConfigError, default_config_path, load_runtime_config


def write_toml(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    os.chmod(p, 0o600)
    return p


BASE_TOML = """\
config_version = 1

[site]
debug = true
allowed_hosts = ["localhost", "127.0.0.1"]
language_code = "hu"
time_zone = "Europe/Budapest"

[meta]
base_url = "https://api.meta.ai/v1"
model = "muse-spark-1.3"
api_key = ""

[limits]
advice_max_per_hour_per_farm = 5
advice_max_per_day_per_farm = 20
advice_global_max_per_day = 200
advice_request_timeout_seconds = 30
advice_pending_stale_minutes = 15
advice_max_input_chars = 8000
advice_max_output_tokens = 6000
chat_request_timeout_seconds = 45
chat_max_output_tokens = 6000
chat_max_input_chars = 15000
chat_max_message_chars = 2000
"""


class LoaderPresenceTests(SimpleTestCase):
    def test_loader_entrypoint_exists(self):
        mod = importlib.import_module("config.runtime")
        self.assertTrue(
            hasattr(mod, "load_runtime_config"),
            "config.runtime.load_runtime_config hianyzik",
        )


class MissingFileTests(SimpleTestCase):
    def test_missing_file_preserves_defaults(self):
        r = load_runtime_config(
            config_path="/tmp/nonexistent-portal-config-tdd.toml",
            environ={},
            dedicated_environ={},
        )
        self.assertFalse(r["config_found"])
        self.assertIsNone(r["config_path"])
        self.assertFalse(r["DJANGO_DEBUG"])
        self.assertEqual(r["DJANGO_ALLOWED_HOSTS"], ["127.0.0.1", "localhost"])
        self.assertEqual(r["LANGUAGE_CODE"], "hu")
        self.assertEqual(r["TIME_ZONE"], "Europe/Budapest")
        self.assertEqual(r["META_API_BASE_URL"], "https://api.meta.ai/v1")
        self.assertEqual(r["META_MODEL"], "muse-spark-1.3")
        self.assertEqual(r["META_API_KEY"], "")
        self.assertEqual(r["ADVICE_MAX_PER_HOUR_PER_FARM"], 5)
        self.assertEqual(r["CHAT_REQUEST_TIMEOUT_SECONDS"], 45.0)

    def test_missing_file_honors_legacy_env(self):
        r = load_runtime_config(
            config_path="/tmp/nonexistent-portal-config-tdd.toml",
            environ={"META_MODEL": "legacy-model-x",
                     "ADVICE_MAX_PER_HOUR_PER_FARM": "7"},
            dedicated_environ={},
        )
        self.assertEqual(r["META_MODEL"], "legacy-model-x")
        self.assertEqual(r["ADVICE_MAX_PER_HOUR_PER_FARM"], 7)

    def test_loader_does_not_mutate_os_environ(self):
        before = dict(os.environ)
        p = write_toml(self._tmp_dir(), "c.toml", BASE_TOML)
        load_runtime_config(config_path=str(p), environ={},
                            dedicated_environ={})
        self.assertEqual(dict(os.environ), before)

    def _tmp_dir(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="portal-cfg-tdd-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return __import__("pathlib").Path(d)


class PrecedenceTests(SimpleTestCase):
    def test_toml_beats_systemd_promoted_legacy_env(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML.replace(
            'api_key = ""', 'api_key = "toml-key"'))
        r = load_runtime_config(
            config_path=str(p),
            environ={"META_API_KEY": "systemd-legacy-key"},
            dedicated_environ={},
        )
        self.assertEqual(r["META_API_KEY"], "toml-key")

    def test_dedicated_env_beats_toml(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML.replace(
            'api_key = ""', 'api_key = "toml-key"'))
        r = load_runtime_config(
            config_path=str(p),
            environ={"META_API_KEY": "systemd-legacy-key"},
            dedicated_environ={"PORTAL_META_API_KEY": "dedicated-key"},
        )
        self.assertEqual(r["META_API_KEY"], "dedicated-key")

    def test_blank_toml_api_key_falls_back_to_legacy(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML)
        r = load_runtime_config(
            config_path=str(p),
            environ={"META_API_KEY": "legacy-key"},
            dedicated_environ={},
        )
        self.assertEqual(r["META_API_KEY"], "legacy-key")

    def test_explicit_empty_dedicated_key_disables_legacy_fallback(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML)
        r = load_runtime_config(
            config_path=str(p),
            environ={"META_API_KEY": "legacy-key"},
            dedicated_environ={"PORTAL_META_API_KEY": ""},
        )
        self.assertEqual(r["META_API_KEY"], "")

    def test_debug_chain_dedicated_over_toml_over_legacy(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML)
        r = load_runtime_config(
            config_path=str(p),
            environ={"DJANGO_DEBUG": "False"},
            dedicated_environ={},
        )
        self.assertTrue(r["DJANGO_DEBUG"])
        with self.assertRaises(ConfigError):
            load_runtime_config(
                config_path=str(p),
                environ={"DJANGO_DEBUG": "False"},
                dedicated_environ={"PORTAL_DJANGO_DEBUG": ""},
            )

    def test_site_keys_absent_do_not_override_legacy(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", '[meta]\napi_key = "k"\n')
        r = load_runtime_config(
            config_path=str(p),
            environ={"DJANGO_DEBUG": "True",
                     "DJANGO_ALLOWED_HOSTS": "example-legacy.invalid"},
            dedicated_environ={},
        )
        self.assertTrue(r["DJANGO_DEBUG"])
        self.assertEqual(r["DJANGO_ALLOWED_HOSTS"], ["example-legacy.invalid"])
        self.assertEqual(r["META_API_KEY"], "k")


class DedicatedDebugStrictTests(SimpleTestCase):
    def _load(self, dedicated, environ=None, text='[meta]\napi_key = ""\n'):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", text)
        return load_runtime_config(config_path=str(p),
                                   environ=environ or {},
                                   dedicated_environ=dedicated)

    def test_dedicated_debug_accepts_only_strict_bools(self):
        for raw, want in (("1", True), ("true", True), ("yes", True),
                          ("True", True), ("YES", True),
                          ("0", False), ("false", False), ("no", False),
                          ("False", False), ("NO", False),
                          ("  true  ", True), ("  no  ", False)):
            r = self._load({"PORTAL_DJANGO_DEBUG": raw})
            self.assertEqual(r["DJANGO_DEBUG"], want, msg=raw)

    def test_dedicated_debug_rejects_gibberish_and_empty(self):
        for raw in ("treu", "2", "", "   ", "on", "off", "igen", "1.0"):
            with self.assertRaises(ConfigError, msg=repr(raw)):
                self._load({"PORTAL_DJANGO_DEBUG": raw})

    def test_legacy_debug_stays_lenient(self):
        self.assertTrue(self._load({}, {"DJANGO_DEBUG": "True"})["DJANGO_DEBUG"])
        self.assertFalse(self._load({}, {"DJANGO_DEBUG": "treu"})["DJANGO_DEBUG"])
        self.assertFalse(self._load({}, {"DJANGO_DEBUG": ""})["DJANGO_DEBUG"])


class BlankKeyNoTransportTests(SimpleTestCase):
    def _no_key_clients(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML)
        r = load_runtime_config(config_path=str(p), environ={},
                                dedicated_environ={})
        self.assertEqual(r["META_API_KEY"], "")
        return r

    def test_blank_placeholder_advice_client_raises_without_transport(self):
        from portal.provider import MetaProviderClient, ProviderError
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        r = self._no_key_clients()
        client = MetaProviderClient(
            api_key=r["META_API_KEY"], base_url=r["META_API_BASE_URL"],
            model=r["META_MODEL"], timeout=r["ADVICE_REQUEST_TIMEOUT_SECONDS"],
            transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError):
            client.generate_advice({})
        self.assertEqual(calls, [])

    def test_blank_placeholder_chat_client_raises_without_transport(self):
        from portal.chat import MetaChatClient
        from portal.provider import ProviderError
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        r = self._no_key_clients()
        client = MetaChatClient(
            api_key=r["META_API_KEY"], base_url=r["META_API_BASE_URL"],
            model=r["META_MODEL"], timeout=r["CHAT_REQUEST_TIMEOUT_SECONDS"],
            transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError):
            client.generate_chat("hello", context="ctx")
        self.assertEqual(calls, [])


class ValidationTests(SimpleTestCase):
    def _load_text(self, text, environ=None, dedicated=None):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", text)
        return load_runtime_config(config_path=str(p),
                                   environ=environ or {},
                                   dedicated_environ={} if dedicated is None else dedicated)

    def test_unknown_section_and_key_fail_closed(self):
        with self.assertRaises(ConfigError):
            self._load_text('[bogus]\nx = 1\n')
        with self.assertRaises(ConfigError):
            self._load_text('[site]\ndebug = false\nbogus_key = 1\n')

    def test_debug_bool_not_int(self):
        with self.assertRaises(ConfigError):
            self._load_text('[site]\ndebug = 1\n')

    def test_allowed_hosts_rejects_wildcard(self):
        with self.assertRaises(ConfigError):
            self._load_text('[site]\nallowed_hosts = ["*"]\n')

    def test_language_and_timezone_validated(self):
        with self.assertRaises(ConfigError):
            self._load_text('[site]\nlanguage_code = "de"\n')
        with self.assertRaises(ConfigError):
            self._load_text('[site]\ntime_zone = "Mars/Olympus"\n')
        r = self._load_text('[site]\nlanguage_code = "en"\ntime_zone = "Europe/Budapest"\n')
        self.assertEqual(r["LANGUAGE_CODE"], "en")

    def test_url_rejects_http_localhost_private_userinfo_query(self):
        for bad in ('[meta]\nbase_url = "http://api.meta.ai/v1"\n',
                    '[meta]\nbase_url = "https://localhost/v1"\n',
                    '[meta]\nbase_url = "https://127.0.0.1/v1"\n',
                    '[meta]\nbase_url = "https://10.0.0.1/v1"\n',
                    '[meta]\nbase_url = "https://user:pass@api.meta.ai/v1"\n',
                    '[meta]\nbase_url = "https://api.meta.ai/v1?x=1"\n',
                    '[meta]\nbase_url = "https://api.meta.ai/v1#frag"\n',
                    '[meta]\nbase_url = "https://api.meta.ai/chat/completions"\n'):
            with self.assertRaises(ConfigError, msg=bad):
                self._load_text(bad)

    def test_hardened_url_and_key_rejections(self):
        bad_urls = (
            "https://api.meta\n.ai/v1",
            "https://api.meta.ai/v1\n/evil",
            "https://api.meta.ai\t/v1",
            "https://api.meta.ai/v 1",
            "https://myapp.localhost/v1",
            "https://printer.local/v1",
            "https://127.1/v1",
            "https://10.1/v1",
            "https://10.0.0.300/v1",
            "https://1.2.3.4.5/v1",
            "https://2130706433/v1",
            "https://0x7f000001/v1",
        )
        for url in bad_urls:
            toml_url = url.replace("\n", "\\n").replace("\t", "\\t")
            with self.assertRaises(ConfigError, msg=repr(url)):
                self._load_text('[meta]\nbase_url = "%s"\n' % toml_url)
        for url in ("https://api.meta.ai/v1", "https://models.example.org/v1"):
            r = self._load_text('[meta]\nbase_url = "%s"\n' % url)
            self.assertEqual(r["META_API_BASE_URL"], url)
        with self.assertRaises(ConfigError):
            self._load_text('[meta]\nbase_url = "https://api.meta.ai/v1"\n',
                            dedicated={"PORTAL_META_API_BASE_URL":
                                       "https://evil.local/v1"})
        bad_keys = ("k\ney", "k\r\ney", "k" + chr(0) + "ey", "k\tey")
        for key in bad_keys:
            toml_key = (key.replace("\n", "\\n").replace("\r", "\\r")
                           .replace(chr(0), "\\u0000").replace("\t", "\\t"))
            with self.assertRaises(ConfigError, msg=repr(key)):
                self._load_text('[meta]\napi_key = "%s"\n' % toml_key)
            with self.assertRaises(ConfigError, msg="dedicated:" + repr(key)):
                self._load_text('[meta]\napi_key = ""\n',
                                dedicated={"PORTAL_META_API_KEY": key})
        for key in bad_keys:
            r = self._load_text('[meta]\napi_key = ""\n',
                                environ={"META_API_KEY": key})
            self.assertEqual(r["META_API_KEY"], key)

    def test_model_nonempty_bounded_no_controls(self):
        with self.assertRaises(ConfigError):
            self._load_text('[meta]\nmodel = ""\n')
        with self.assertRaises(ConfigError):
            self._load_text('[meta]\nmodel = "%s"\n' % ("m" * 121))
        with self.assertRaises(ConfigError):
            self._load_text('[meta]\nmodel = "a\\u0000b"\n')

    def test_limits_positive_bounded(self):
        with self.assertRaises(ConfigError):
            self._load_text('[limits]\nadvice_max_per_hour_per_farm = 0\n')
        with self.assertRaises(ConfigError):
            self._load_text('[limits]\nadvice_max_per_hour_per_farm = -3\n')
        with self.assertRaises(ConfigError):
            self._load_text('[limits]\nchat_request_timeout_seconds = 0\n')
        with self.assertRaises(ConfigError):
            self._load_text('[limits]\nchat_max_output_tokens = 1000000\n')
        with self.assertRaises(ConfigError):
            self._load_text('[limits]\nadvice_request_timeout_seconds = true\n')

    def test_malformed_and_unreadable_fail_safe(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = d / "bad.toml"
        p.write_text('[site\ndebug = \n')
        os.chmod(p, 0o600)
        with self.assertRaises(ConfigError):
            load_runtime_config(config_path=str(p), environ={},
                                dedicated_environ={})
        sub = d / "adir"
        sub.mkdir()
        with self.assertRaises(ConfigError):
            load_runtime_config(config_path=str(sub), environ={},
                                dedicated_environ={})

    def test_symlink_and_insecure_permissions_rejected(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        real = write_toml(d, "real.toml", BASE_TOML)
        link = d / "link.toml"
        try:
            link.symlink_to(real)
        except OSError:
            self.skipTest("symlink nem tamogatott")
            return
        with self.assertRaises(ConfigError):
            load_runtime_config(config_path=str(link), environ={},
                                dedicated_environ={})
        open_file = d / "open.toml"
        open_file.write_text(BASE_TOML)
        os.chmod(open_file, 0o644)
        with self.assertRaises(ConfigError):
            load_runtime_config(config_path=str(open_file), environ={},
                                dedicated_environ={})

    def test_default_path_is_basedir_portal_config_toml(self):
        from django.conf import settings
        p = default_config_path()
        self.assertEqual(p.name, "portal-config.toml")
        static_root = str(settings.STATIC_ROOT)
        self.assertNotEqual(str(p), static_root)
        self.assertFalse(str(p).startswith(static_root.rstrip("/") + "/"))


class RedactionTests(SimpleTestCase):
    def test_errors_mention_field_only_and_suppress_chain(self):
        sentinel = "SENTINEL-SECRET-9zqx"
        cases = [
            '[site]\nallowed_hosts = ["*-%s"]\n' % sentinel,
            '[meta]\nbase_url = "https://api.meta.ai/v1?x=%s"\n' % sentinel,
            '[meta]\nmodel = "%s"\n' % (sentinel + "m" * 120),
        ]
        for text in cases:
            import pathlib
            import tempfile
            d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
            self.addCleanup(__import__("shutil").rmtree, str(d), True)
            p = write_toml(d, "c.toml", text)
            with self.assertRaises(ConfigError) as cm:
                load_runtime_config(config_path=str(p), environ={},
                                    dedicated_environ={})
            self.assertNotIn(sentinel, str(cm.exception))
            self.assertIsNone(cm.exception.__cause__)
        raw = 'this is not [valid toml %s {{{' % sentinel
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", raw)
        with self.assertRaises(ConfigError) as cm:
            load_runtime_config(config_path=str(p), environ={},
                                dedicated_environ={})
        self.assertNotIn(sentinel, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_loading_emits_no_secret_bearing_logs(self):
        records = []

        class Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        sentinel = "SENTINEL-SECRET-7abc"
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", BASE_TOML.replace(
            'api_key = ""', 'api_key = "%s"' % sentinel))
        root = logging.getLogger()
        h = Cap()
        root.addHandler(h)
        try:
            load_runtime_config(config_path=str(p), environ={},
                                dedicated_environ={})
        finally:
            root.removeHandler(h)
        for rec in records:
            self.assertNotIn(sentinel, rec.getMessage())


class UnknownNameRedactionTests(SimpleTestCase):
    def _load_text(self, text):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        p = write_toml(d, "c.toml", text)
        return load_runtime_config(config_path=str(p), environ={},
                                   dedicated_environ={})

    def test_unknown_section_name_never_echoed(self):
        sentinel = "SENTINEL-SECRET-9zqx"
        with self.assertRaises(ConfigError) as cm:
            self._load_text('[%s]\nx = 1\n' % sentinel)
        self.assertEqual(str(cm.exception), "unknown config field")
        self.assertNotIn(sentinel, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)

    def test_unknown_key_name_never_echoed(self):
        sentinel = "SENTINEL-SECRET-9zqx"
        with self.assertRaises(ConfigError) as cm:
            self._load_text('[site]\n"%s" = 1\n' % sentinel)
        self.assertEqual(str(cm.exception), "unknown config field")
        self.assertNotIn(sentinel, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)


class GitIgnoreTests(SimpleTestCase):
    def test_private_config_is_git_ignored(self):
        from django.conf import settings
        gitignore = (settings.BASE_DIR / ".gitignore").read_text()
        self.assertIn("/portal-config.toml", gitignore.splitlines())


class ProviderIntegrationTests(SimpleTestCase):
    DUMMY_KEY = "DUMMY-KEY-FOR-TESTS-ONLY"

    def _resolved(self):
        import pathlib
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="portal-cfg-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(d), True)
        text = BASE_TOML.replace('model = "muse-spark-1.3"',
                                 'model = "test-portal-model"')
        p = write_toml(d, "c.toml", text)
        return load_runtime_config(
            config_path=str(p), environ={},
            dedicated_environ={"PORTAL_META_API_KEY": self.DUMMY_KEY})

    def test_advice_client_uses_loaded_endpoint_model_payload_timeout(self):
        from portal.provider import MetaProviderClient
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode())
            seen["auth"] = request.headers.get("authorization")
            payload = {"ok": True}
            content = json.dumps(payload)
            return httpx.Response(200, json={
                "model": "test-actual", "usage": {"prompt_tokens": 1},
                "choices": [{"message": {"content": content}}]})

        r = self._resolved()
        client = MetaProviderClient(
            api_key=r["META_API_KEY"], base_url=r["META_API_BASE_URL"],
            model=r["META_MODEL"], timeout=r["ADVICE_REQUEST_TIMEOUT_SECONDS"],
            transport=httpx.MockTransport(handler))
        out = client.generate_advice({"farm": "adat"})
        self.assertEqual(seen["url"], "https://api.meta.ai/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], "test-portal-model")
        self.assertEqual(seen["body"]["max_tokens"], r["ADVICE_MAX_OUTPUT_TOKENS"])
        self.assertEqual(seen["auth"], "Bearer %s" % self.DUMMY_KEY)
        self.assertEqual(client.timeout, r["ADVICE_REQUEST_TIMEOUT_SECONDS"])
        self.assertEqual(out["ok"], True)

    def test_chat_client_uses_loaded_endpoint_model_payload_timeout(self):
        from portal.chat import MetaChatClient
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode())
            seen["auth"] = request.headers.get("authorization")
            content = json.dumps({"answer": "ok", "actions": [], "links": []})
            return httpx.Response(200, json={
                "model": "test-actual",
                "choices": [{"message": {"content": content}}]})

        r = self._resolved()
        client = MetaChatClient(
            api_key=r["META_API_KEY"], base_url=r["META_API_BASE_URL"],
            model=r["META_MODEL"], timeout=r["CHAT_REQUEST_TIMEOUT_SECONDS"],
            transport=httpx.MockTransport(handler))
        out = client.generate_chat("kerdes", context="kornyezet")
        self.assertEqual(seen["url"], "https://api.meta.ai/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], "test-portal-model")
        self.assertEqual(seen["body"]["max_tokens"], r["CHAT_MAX_OUTPUT_TOKENS"])
        self.assertEqual(seen["auth"], "Bearer %s" % self.DUMMY_KEY)
        self.assertEqual(client.timeout, r["CHAT_REQUEST_TIMEOUT_SECONDS"])
        self.assertEqual(out["answer"], "ok")


class FreshSettingsTests(SimpleTestCase):
    """Blockers 3+4: friss settings-import + alapertelmezett kliensek.

    Friss (kulon ertelmezo) betoltes a config/settings.py + config/runtime.py
    MASOLATAN, tempfile csomag-gyokerrel: ervenytelen legacy
    ADVICE_MAX_OUTPUT_TOKENS + ervenyes TOML (egyedi endpoint/model/key/
    timeout) eseten is sikeresen tolt, sentinel sehol. A default-konstruat
    tanacs/chat kliensek (csak MockTransport injektalva) az effektv Django
    settingsbol olvasnak. Elo halozat/hivas nelkul, valodi .env nelkul.
    """

    CUSTOM_BASE = "https://models.example.org/v1"
    CUSTOM_MODEL = "test-model"
    DUMMY_KEY = "DUMMY-KEY-FOR-TDD-ONLY-0123456789"
    DUMMY_SECRET = "DUMMY-SECRET-FOR-TDD-0123456789ABCDEF"
    SENTINEL = "SENTINEL-secret-invalid-int"

    TOML_TEXT = """\
config_version = 1

[site]
debug = false
allowed_hosts = ["localhost", "127.0.0.1"]
language_code = "hu"
time_zone = "Europe/Budapest"

[meta]
base_url = "https://models.example.org/v1"
model = "test-model"
api_key = "DUMMY-KEY-FOR-TDD-ONLY-0123456789"

[limits]
advice_max_per_hour_per_farm = 5
advice_max_per_day_per_farm = 20
advice_global_max_per_day = 200
advice_request_timeout_seconds = 31
advice_pending_stale_minutes = 15
advice_max_input_chars = 8000
advice_max_output_tokens = 6001
chat_request_timeout_seconds = 46
chat_max_output_tokens = 6002
chat_max_input_chars = 15000
chat_max_message_chars = 2000
"""

    CHILD_SCRIPT = """\
import json
import os
import runpy
import sys

pkgroot, orig = sys.argv[1], sys.argv[2]
sys.path.insert(0, pkgroot)
sys.path.append(orig)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import config as _cfg

assert _cfg.__file__.startswith(pkgroot), _cfg.__file__

ns = runpy.run_path(os.path.join(pkgroot, "config", "settings.py"))
assert ns["ADVICE_MAX_OUTPUT_TOKENS"] == 6001, ns.get("ADVICE_MAX_OUTPUT_TOKENS")
assert ns["CHAT_MAX_OUTPUT_TOKENS"] == 6002, ns.get("CHAT_MAX_OUTPUT_TOKENS")
assert ns["META_API_BASE_URL"] == "https://models.example.org/v1", ns.get("META_API_BASE_URL")
assert ns["META_MODEL"] == "test-model", ns.get("META_MODEL")
assert ns["META_API_KEY"] == "DUMMY-KEY-FOR-TDD-ONLY-0123456789"
assert ns["ADVICE_REQUEST_TIMEOUT_SECONDS"] == 31.0, ns.get("ADVICE_REQUEST_TIMEOUT_SECONDS")
assert ns["CHAT_REQUEST_TIMEOUT_SECONDS"] == 46.0, ns.get("CHAT_REQUEST_TIMEOUT_SECONDS")

import django

django.setup()

from django.conf import settings as dj

assert dj.ADVICE_MAX_OUTPUT_TOKENS == 6001, dj.ADVICE_MAX_OUTPUT_TOKENS
assert dj.CHAT_MAX_OUTPUT_TOKENS == 6002, dj.CHAT_MAX_OUTPUT_TOKENS
assert dj.META_API_BASE_URL == "https://models.example.org/v1", dj.META_API_BASE_URL
assert dj.META_MODEL == "test-model", dj.META_MODEL
assert dj.META_API_KEY == "DUMMY-KEY-FOR-TDD-ONLY-0123456789"
assert dj.ADVICE_REQUEST_TIMEOUT_SECONDS == 31.0, dj.ADVICE_REQUEST_TIMEOUT_SECONDS
assert dj.CHAT_REQUEST_TIMEOUT_SECONDS == 46.0, dj.CHAT_REQUEST_TIMEOUT_SECONDS

import httpx

from portal.chat import MetaChatClient
from portal.provider import MetaProviderClient

advice_seen = {}


def advice_handler(request):
    advice_seen["url"] = str(request.url)
    advice_seen["body"] = json.loads(request.content.decode())
    advice_seen["auth"] = request.headers.get("authorization")
    return httpx.Response(200, json={
        "model": "test-actual", "usage": {},
        "choices": [{"message": {"content": json.dumps({"ok": True})}}]})


advice = MetaProviderClient(transport=httpx.MockTransport(advice_handler))
assert advice.base_url == "https://models.example.org/v1", advice.base_url
assert advice.model == "test-model", advice.model
assert advice.api_key == "DUMMY-KEY-FOR-TDD-ONLY-0123456789"
assert advice.timeout == 31.0, advice.timeout
aout = advice.generate_advice({"farm": "adat"})
assert aout["ok"] is True, aout
assert advice_seen["url"] == "https://models.example.org/v1/chat/completions", advice_seen
assert advice_seen["body"]["model"] == "test-model", advice_seen
assert advice_seen["body"]["max_tokens"] == 6001, advice_seen
assert advice_seen["auth"] == "Bearer DUMMY-KEY-FOR-TDD-ONLY-0123456789"

chat_seen = {}


def chat_handler(request):
    chat_seen["url"] = str(request.url)
    chat_seen["body"] = json.loads(request.content.decode())
    chat_seen["auth"] = request.headers.get("authorization")
    content = json.dumps({"answer": "ok", "actions": [], "links": []})
    return httpx.Response(200, json={
        "model": "test-actual",
        "choices": [{"message": {"content": content}}]})


chat = MetaChatClient(transport=httpx.MockTransport(chat_handler))
assert chat.base_url == "https://models.example.org/v1", chat.base_url
assert chat.model == "test-model", chat.model
assert chat.api_key == "DUMMY-KEY-FOR-TDD-ONLY-0123456789"
assert chat.timeout == 46.0, chat.timeout
cout = chat.generate_chat("kerdes", context="kornyezet")
assert cout["answer"] == "ok", cout
assert chat_seen["url"] == "https://models.example.org/v1/chat/completions", chat_seen
assert chat_seen["body"]["model"] == "test-model", chat_seen
assert chat_seen["body"]["max_tokens"] == 6002, chat_seen
assert chat_seen["auth"] == "Bearer DUMMY-KEY-FOR-TDD-ONLY-0123456789"

print("CHILD-OK advice_url=%s chat_url=%s model=%s advice_t=%s chat_t=%s" % (
    advice_seen["url"], chat_seen["url"], advice.model,
    advice.timeout, chat.timeout))
"""

    def test_fresh_settings_invalid_legacy_bypassed_and_default_clients_use_toml(self):
        import pathlib
        import subprocess
        import sys
        import tempfile
        from django.conf import settings as live

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="portal-settings-tdd-"))
        self.addCleanup(__import__("shutil").rmtree, str(tmp), True)
        pkgroot = tmp / "pkg"
        (pkgroot / "config").mkdir(parents=True)
        root = pathlib.Path(live.BASE_DIR)
        for name in ("__init__.py", "settings.py", "runtime.py"):
            (pkgroot / "config" / name).write_bytes(
                (root / "config" / name).read_bytes())
        toml = pkgroot / "portal-config.toml"
        toml.write_text(self.TOML_TEXT)
        os.chmod(toml, 0o600)
        dotenv = pkgroot / ".env"
        dotenv.write_text("DJANGO_SECRET_KEY=%s\n" % self.DUMMY_SECRET)
        os.chmod(dotenv, 0o600)
        child = tmp / "child_check.py"
        child.write_text(self.CHILD_SCRIPT)

        env = dict(os.environ)
        for key in list(env):
            if (key.startswith("PORTAL_") or key.startswith("META_")
                    or key.startswith("ADVICE_") or key.startswith("CHAT_")
                    or key.startswith("DJANGO_")):
                del env[key]
        env["ADVICE_MAX_OUTPUT_TOKENS"] = self.SENTINEL
        proc = subprocess.run(
            [sys.executable, str(child), str(pkgroot), str(root)],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(tmp))
        self.assertEqual(proc.returncode, 0,
                         "friss settings-import elbukott\nSTDOUT:\n%s\nSTDERR:\n%s"
                         % (proc.stdout, proc.stderr))
        self.assertNotIn(self.SENTINEL, proc.stderr)
        self.assertIn("CHILD-OK", proc.stdout)
