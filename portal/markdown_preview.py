"""Safe Markdown preview reader for private document blobs.

Untrusted upload content is read through a directory file descriptor
relative to the private root, with symlink and non-regular files
rejected. Any failure returns a generic unavailable result without
leaking paths or storage names.
"""
import logging
import os
import re
import stat

logger = logging.getLogger(__name__)

PREVIEW_MAX_BYTES = 256 * 1024
PREVIEW_EXTENSIONS = frozenset({".md", ".markdown"})

_STORAGE_RE = re.compile(r"^[0-9a-fA-F]{32}\.(?:md|markdown)$")


def is_previewable_extension(extension):
    """Check whether a document extension gets a Markdown preview."""
    return str(extension or "").lower() in PREVIEW_EXTENSIONS


def _unavailable():
    return {"available": False, "html": "", "truncated": False}


def load_markdown_preview(storage_name):
    """Read at most the first 256 KiB of a private Markdown blob.

    Returns a dict with keys ``available``, ``html`` (sanitized) and
    ``truncated``. Missing files, symlinks, directories, FIFOs and any
    invalid name map to the generic unavailable result.
    """
    name = storage_name or ""
    if not isinstance(name, str):
        return _unavailable()
    if not name or "/" in name or "\\" in name or "\x00" in name or ".." in name:
        return _unavailable()
    if not _STORAGE_RE.match(name):
        return _unavailable()
    try:
        from .document_storage import private_root
        from .regenerative_policy import render_safe_markdown
        root = private_root()
    except Exception:
        logger.warning("markdown preview: private root unavailable")
        return _unavailable()
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
            name,
            os.O_RDONLY | os.O_NOFOLLOW | nonblock | cloexec,
            dir_fd=root_fd,
        )
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            return _unavailable()
        limit = PREVIEW_MAX_BYTES + 1
        chunks = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        truncated = len(raw) > PREVIEW_MAX_BYTES
        if truncated:
            raw = raw[:PREVIEW_MAX_BYTES]
        text = raw.decode("utf-8-sig", errors="replace")
        html = render_safe_markdown(text)
        return {"available": True, "html": html, "truncated": truncated}
    except Exception:
        logger.warning("markdown preview: unreadable blob")
        return _unavailable()
    finally:
        for to_close in (fd, root_fd):
            if to_close is not None:
                try:
                    os.close(to_close)
                except OSError:
                    pass
