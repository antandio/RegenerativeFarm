import os
import stat
import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
# Explicit PORTAL_* process-env snapshot BEFORE the .env loader:
# a .env file must not forge process-env overrides.
_PORTAL_DEDICATED_SNAPSHOT = {k: v for k, v in os.environ.items() if k.startswith("PORTAL_")}
def _env(name, default=""):
    return os.environ.get(name, default)
# load .env file if present (simple parser, no external dep)
_envfile = BASE_DIR / ".env"
if _envfile.exists():
    for _line in _envfile.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
SECRET_KEY = _env("DJANGO_SECRET_KEY")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError("DJANGO_SECRET_KEY must be set (>=32 chars) via env/.env")
DEBUG = _env("DJANGO_DEBUG", "False").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in _env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
INSTALLED_APPS = ["django.contrib.admin","django.contrib.auth","django.contrib.contenttypes","django.contrib.sessions","django.contrib.messages","django.contrib.staticfiles","portal"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware","whitenoise.middleware.WhiteNoiseMiddleware","django.contrib.sessions.middleware.SessionMiddleware","django.middleware.locale.LocaleMiddleware","django.middleware.common.CommonMiddleware","django.middleware.csrf.CsrfViewMiddleware","django.contrib.auth.middleware.AuthenticationMiddleware","django.contrib.messages.middleware.MessageMiddleware","django.middleware.clickjacking.XFrameOptionsMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND":"django.template.backends.django.DjangoTemplates","DIRS":[],"APP_DIRS":True,"OPTIONS":{"context_processors":["django.template.context_processors.request","django.template.context_processors.i18n","django.contrib.auth.context_processors.auth","django.contrib.messages.context_processors.messages","portal.context_processors.owner_nav", "portal.context_processors.global_farm_selector", "portal.context_processors.page_title"]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default":{"ENGINE":"django.db.backends.sqlite3","NAME": BASE_DIR / "db.sqlite3","OPTIONS":{"timeout":20,"transaction_mode":"IMMEDIATE"}}}
AUTH_USER_MODEL = "portal.User"
AUTH_PASSWORD_VALIDATORS = [{"NAME":"django.contrib.auth.password_validation.MinimumLengthValidator","OPTIONS":{"min_length":10}},{"NAME":"django.contrib.auth.password_validation.CommonPasswordValidator"},{"NAME":"django.contrib.auth.password_validation.NumericPasswordValidator"},{"NAME":"django.contrib.auth.password_validation.UserAttributeSimilarityValidator"}]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
LANGUAGE_CODE = "hu"
LANGUAGES = [("hu", "Magyar"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_COOKIE_NAME = "django_language"
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365
LANGUAGE_COOKIE_SAMESITE = "Lax"
TIME_ZONE = "Europe/Budapest"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/belepes/"
LOGIN_REDIRECT_URL = "/attekintes/"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# Meta / advice config (env only, never logged)
META_API_KEY = _env("META_API_KEY", "")
META_API_BASE_URL = _env("META_API_BASE_URL", "https://api.meta.ai/v1").rstrip("/")
META_MODEL = _env("META_MODEL", "muse-spark-1.3")
# The limit pre-reads above are intentionally missing: raw int()/float()
# conversions are dead duplicates (the runtime block overwrites them
# unconditionally anyway), and on invalid legacy values they crashed
# startup even with valid TOML present. Single parser: load_runtime_config().
# (The three META_* string pre-reads do not crash, keep them.)
# Private TOML runtime config (optional BASE_DIR/portal-config.toml).
# Precedence: PORTAL_<ENV> process env > explicit TOML > legacy env/.env > default.
# Does not modify os.environ, never logs/re-exports secrets.
from config.runtime import load_runtime_config as _load_runtime_config
_RUNTIME = _load_runtime_config(base_dir=BASE_DIR, environ=dict(os.environ), dedicated_environ=_PORTAL_DEDICATED_SNAPSHOT)
DEBUG = _RUNTIME["DJANGO_DEBUG"]
ALLOWED_HOSTS = _RUNTIME["DJANGO_ALLOWED_HOSTS"]
LANGUAGE_CODE = _RUNTIME["LANGUAGE_CODE"]
TIME_ZONE = _RUNTIME["TIME_ZONE"]
META_API_KEY = _RUNTIME["META_API_KEY"]
META_API_BASE_URL = _RUNTIME["META_API_BASE_URL"]
META_MODEL = _RUNTIME["META_MODEL"]
ADVICE_MAX_PER_HOUR_PER_FARM = _RUNTIME["ADVICE_MAX_PER_HOUR_PER_FARM"]
ADVICE_MAX_PER_DAY_PER_FARM = _RUNTIME["ADVICE_MAX_PER_DAY_PER_FARM"]
ADVICE_GLOBAL_MAX_PER_DAY = _RUNTIME["ADVICE_GLOBAL_MAX_PER_DAY"]
ADVICE_REQUEST_TIMEOUT_SECONDS = _RUNTIME["ADVICE_REQUEST_TIMEOUT_SECONDS"]
ADVICE_PENDING_STALE_MINUTES = _RUNTIME["ADVICE_PENDING_STALE_MINUTES"]
ADVICE_MAX_INPUT_CHARS = _RUNTIME["ADVICE_MAX_INPUT_CHARS"]
ADVICE_MAX_OUTPUT_TOKENS = _RUNTIME["ADVICE_MAX_OUTPUT_TOKENS"]
CHAT_REQUEST_TIMEOUT_SECONDS = _RUNTIME["CHAT_REQUEST_TIMEOUT_SECONDS"]
CHAT_MAX_OUTPUT_TOKENS = _RUNTIME["CHAT_MAX_OUTPUT_TOKENS"]
CHAT_MAX_INPUT_CHARS = _RUNTIME["CHAT_MAX_INPUT_CHARS"]
CHAT_MAX_MESSAGE_CHARS = _RUNTIME["CHAT_MAX_MESSAGE_CHARS"]
del _RUNTIME, _PORTAL_DEDICATED_SNAPSHOT
CHAT_PROMPT_VERSION = "chat_prompt_v1"
CHAT_OUTPUT_SCHEMA_VERSION = "chat_output_v1"
TERMS_VERSION = "aszf_v1"
PRIVACY_VERSION = "tajekoztato_v1"
CLOUD_POLICY_VERSION = "cloud_disclosure_v1"
PROMPT_VERSION = "advice_prompt_v1"
OUTPUT_SCHEMA_VERSION = "advice_output_v1"
LOGGING = {"version":1,"disable_existing_loggers":False,"handlers":{"console":{"class":"logging.StreamHandler"}},"root":{"handlers":["console"],"level":"WARNING"}}
# Shared weather cache: default LocMemCache is per-process, so with 2 gunicorn
# workers a refresh on one worker stays invisible to the other. Production
# therefore uses a private FileBasedCache directory (mode 0700, service user
# hermes). The test suite keeps LocMemCache automatically via sys.argv guard,
# so no cache dir is created on test imports.
# NOTE: FileBasedCache pickles values (trusted local host only). The dir is
# inside the repo, never under STATIC_ROOT, never served as static.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
PRIVATE_DOCUMENT_ROOT = BASE_DIR / "private_documents"
WEATHER_CACHE_DIR = BASE_DIR / "weather_cache"
def ensure_private_cache_dir(path):
    p = Path(path)
    if p.is_symlink():
        raise RuntimeError(f"refusing symlinked cache dir: {p}")
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.is_symlink():
        raise RuntimeError(f"refusing symlinked cache dir: {p}")
    os.chmod(p, 0o700)
    if stat.S_IMODE(os.stat(p).st_mode) != 0o700:
        raise RuntimeError(f"cache dir permissions must be 0700: {p}")
    return p
if "test" in sys.argv:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "test-locmem"}}
else:
    ensure_private_cache_dir(WEATHER_CACHE_DIR)
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": str(WEATHER_CACHE_DIR), "TIMEOUT": 300, "OPTIONS": {"MAX_ENTRIES": 1000}}}
