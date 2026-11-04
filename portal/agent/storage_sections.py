"""Safe section reader for private document blobs.

Same trust boundary as `portal.markdown_preview`: reads through a
directory file descriptor relative to the private root, rejects
symlinks and non-regular files, and maps every failure to a generic
unavailable result without leaking paths or storage names. Full raw
bytes (up to 256 KiB) are hashed for manifest pinning; decoded text
stays bounded for display. Oversize blobs are marked unsupported and
never yield a truncated hash as if it were the full hash.
"""
import hashlib
import logging
import os
import stat

logger = logging.getLogger(__name__)

SECTION_MAX_CHARS = 20000

# Full raw-byte cap for sha256 pinning: larger blobs are unsupported.
FULL_BYTES_CAP = 256 * 1024


def _unavailable(reason="unavailable", **extra):
    result = {"available": False, "text": "", "truncated": False,
              "reason": reason}
    result.update(extra)
    return result


def _valid_name(storage_name):
    name = storage_name or ""
    if not isinstance(name, str):
        return False
    if not name or "/" in name or "\\" in name or "\x00" in name \
            or ".." in name:
        return False
    if "." not in name:
        return False
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return False
    from portal.markdown_preview import PREVIEW_EXTENSIONS
    if ("." + ext.lower()) not in PREVIEW_EXTENSIONS:
        return False
    if len(stem) != 32 or any(
            c not in "0123456789abcdefABCDEF" for c in stem):
        return False
    return True


def read_blob_for_manifest(storage_name, max_chars=SECTION_MAX_CHARS):
    """Read raw bytes for hash pinning plus a bounded decoded section.

    Returns ``available``, ``text``, ``truncated``, ``reason``,
    ``sha256`` (hex of the full raw bytes), ``bytes_len``,
    ``oversize`` and ``replaced`` (True when decoding substituted
    characters). Oversize blobs (>256 KiB) return unavailable with
    ``oversize`` True and no hash.
    """
    if not _valid_name(storage_name):
        return _unavailable(oversize=False, sha256="", bytes_len=0,
                            replaced=False, lines=0)
    try:
        from portal.document_storage import private_root
        root = private_root()
    except Exception:
        logger.warning("agent sections: private root unavailable")
        return _unavailable(oversize=False, sha256="", bytes_len=0,
                            replaced=False, lines=0)
    root_fd = None
    fd = None
    try:
        cloexec = getattr(os, "O_CLOEXEC", 0)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        root_fd = os.open(
            os.fspath(root),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec,
        )
        fd = os.open(
            storage_name,
            os.O_RDONLY | os.O_NOFOLLOW | nonblock | cloexec,
            dir_fd=root_fd,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _unavailable(oversize=False, sha256="", bytes_len=0,
                                replaced=False, lines=0)
        limit = FULL_BYTES_CAP + 1
        chunks = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > FULL_BYTES_CAP:
            return _unavailable("oversize-unsupported", oversize=True,
                                sha256="", bytes_len=len(raw),
                                replaced=False, lines=0)
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig", errors="replace")
        replaced = "\ufffd" in text
        full_len = len(text)
        cap = max(1, int(max_chars))
        truncated = full_len > cap
        if truncated:
            text = text[:cap]
        return {"available": True, "text": text, "truncated": truncated,
                "reason": "", "sha256": digest, "bytes_len": len(raw),
                "oversize": False, "replaced": replaced,
                "lines": text.count("\n") + (1 if text else 0)}
    except Exception:
        logger.warning("agent sections: unreadable blob")
        return _unavailable(oversize=False, sha256="", bytes_len=0,
                            replaced=False, lines=0)
    finally:
        for to_close in (fd, root_fd):
            if to_close is not None:
                try:
                    os.close(to_close)
                except OSError:
                    pass


def read_text_section(storage_name, max_chars=SECTION_MAX_CHARS,
                       offset_chars=0):
    """Read a bounded text window of a private Markdown blob.

    Returns a dict with keys ``available``, ``text``, ``truncated`` and
    ``reason``. Only server-generated ``[0-9a-f]{32}.md|markdown`` names
    under the private root are readable; everything else (including
    symlinks, directories and FIFOs) maps to unavailable.
    """
    blob = read_blob_for_manifest(storage_name, max_chars=max_chars)
    text = blob.get("text", "")
    if offset_chars:
        try:
            text = text[int(offset_chars):]
        except (TypeError, ValueError):
            text = ""
    return {"available": blob["available"], "text": text,
            "truncated": blob["truncated"],
            "reason": blob.get("reason", "")}
