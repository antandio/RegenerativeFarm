"""Task8 private document storage: fixed root, server-side validation, UUID names.

No remote fetching, no parser execution, no LLM: files are stored only
and served back byte-identical as attachments.
"""
import hashlib
import io
import logging
import os
import stat
import uuid
import zipfile
from pathlib import Path, PurePath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

DOCUMENT_MAX_BYTES = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = frozenset({".pdf", ".txt", ".md", ".markdown", ".csv", ".docx"})
EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
ALLOWED_CONTENT_TYPES = frozenset(EXTENSION_CONTENT_TYPES.values())

DOCX_REQUIRED = ("[Content_Types].xml", "word/document.xml")
DOCX_MAX_MEMBERS = 500
DOCX_MAX_UNCOMPRESSED_TOTAL = 50 * 1024 * 1024
DOCX_MAX_MEMBER_BYTES = 20 * 1024 * 1024
DOCX_MAX_RATIO = 100


def private_root():
    """Fail closed: any chmod/stat error propagates, and a final mode
    other than 0700 raises instead of serving from a loose root."""
    root = Path(settings.PRIVATE_DOCUMENT_ROOT)
    if root.is_symlink():
        raise RuntimeError(f"refusing symlinked document root: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    if stat.S_IMODE(os.stat(root).st_mode) != 0o700:
        raise RuntimeError(f"document root permissions must be 0700: {root}")
    if root.is_symlink():
        raise RuntimeError(f"refusing symlinked document root: {root}")
    return root


def sanitize_original_filename(name):
    base = PurePath(name or "").name
    cleaned = "".join(c for c in base if c not in ("\x00", "/", "\\") and ord(c) >= 32 and ord(c) != 127)
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = "file"
    return cleaned[:255]


def classify_extension(filename):
    ext = PurePath(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(_("Nem támogatott fájltípus. Engedélyezett: PDF, TXT, MD, CSV, DOCX."))
    return ext


def _reject(msg):
    raise ValidationError(msg)


def validate_text_bytes(data):
    data = bytes(data)
    if b"\x00" in data:
        _reject(_("A szöveges fájl nem tartalmazhat NUL-bájtot."))
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        _reject(_("A szöveges fájlnak UTF-8 kódolásúnak kell lennie."))


def validate_docx_bytes(data):
    if len(data) < 4 or data[:4] != b"PK\x03\x04":
        _reject(_("Érvénytelen DOCX-aláírás."))
    try:
        buf = io.BytesIO(bytes(data))
        if not zipfile.is_zipfile(buf):
            _reject(_("Érvénytelen DOCX-archívum."))
        buf.seek(0)
        with zipfile.ZipFile(buf) as z:
            infos = z.infolist()
            if len(infos) > DOCX_MAX_MEMBERS:
                _reject(_("A DOCX túl sok fájlt tartalmaz."))
            names = [i.filename for i in infos]
            for nm in names:
                low = nm.lower()
                if nm.startswith(("/", "\\")) or ".." in PurePath(nm).parts:
                    _reject(_("A DOCX érvénytelen útvonalat tartalmaz."))
                if "\\" in nm:
                    _reject(_("A DOCX érvénytelen útvonalat tartalmaz."))
                if "vbaproject" in low or low.startswith("word/vba"):
                    _reject(_("A DOCX makrót tartalmaz, nem tölthető fel."))
            for i in infos:
                if i.flag_bits & 0x1:
                    _reject(_("Titkosított DOCX nem tölthető fel."))
                if i.file_size > DOCX_MAX_MEMBER_BYTES:
                    _reject(_("A DOCX egyik tagja túl nagy."))
            total = sum(i.file_size for i in infos)
            if total > DOCX_MAX_UNCOMPRESSED_TOTAL:
                _reject(_("A DOCX kicsomagolt mérete túl nagy."))
            if len(data) > 0 and total // max(len(data), 1) > DOCX_MAX_RATIO:
                _reject(_("A DOCX tömörítési aránya gyanús."))
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                _reject(_("Érvénytelen DOCX-szerkezet."))
    except ValidationError:
        raise
    except Exception:
        _reject(_("Érvénytelen DOCX-archívum."))


def validate_upload(uploaded):
    size = getattr(uploaded, "size", None)
    if size is None:
        uploaded.seek(0, 2)
        size = uploaded.tell()
        uploaded.seek(0)
    if size is None or size <= 0:
        _reject(_("Üres fájl nem tölthető fel."))
    if size > DOCUMENT_MAX_BYTES:
        _reject(_("A fájl túl nagy (legfeljebb 10 MiB)."))
    ext = classify_extension(getattr(uploaded, "name", ""))
    try:
        uploaded.seek(0)
    except Exception:
        pass
    chunks = uploaded.read()
    data = bytes(chunks)
    if len(data) != size:
        size = len(data)
        if size > DOCUMENT_MAX_BYTES:
            _reject(_("A fájl túl nagy (legfeljebb 10 MiB)."))
    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            _reject(_("Érvénytelen PDF-aláírás."))
    elif ext == ".docx":
        validate_docx_bytes(data)
    else:
        validate_text_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    return {
        "extension": ext,
        "content_type": EXTENSION_CONTENT_TYPES[ext],
        "size_bytes": size,
        "sha256": sha256,
        "data": data,
        "original_filename": sanitize_original_filename(getattr(uploaded, "name", "")),
    }


def storage_path(storage_name):
    root = private_root()
    if "/" in storage_name or "\\" in storage_name or ".." in storage_name:
        raise ValueError("invalid storage name")
    p = root / storage_name
    try:
        if p.resolve().parent != root.resolve():
            raise ValueError("invalid storage name")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid storage name") from exc
    return p


def write_new_file(storage_name, data):
    """Fail closed: if the file cannot be hardened to 0600, remove the
    partial file and raise so no loose or half-written blob persists."""
    path = storage_path(storage_name)
    if path.exists():
        raise ValidationError(_("Belső tárolási hiba."))
    with open(path, "xb") as fh:
        fh.write(bytes(data))
    try:
        os.chmod(path, 0o600)
        if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
            raise RuntimeError(f"document file permissions must be 0600: {path}")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def new_storage_name(extension):
    return f"{uuid.uuid4().hex}{extension}"


def delete_storage_file(storage_name):
    try:
        path = storage_path(storage_name)
    except ValueError:
        logger.warning("document delete: invalid storage name refused")
        return
    try:
        path.unlink()
    except FileNotFoundError:
        logger.info("document delete: file already missing")
    except IsADirectoryError:
        logger.warning("document delete: storage path is a directory, refused")
