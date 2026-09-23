"""File-type detection: extension as first pass, magic bytes as calibration
(DESIGN.md §6.5).  Never trust the extension alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Extension -> level-2 subtype (first pass).
_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".txt": "text_plain", ".md": "text_plain", ".markdown": "text_plain",
    ".log": "text_plain", ".csv": "text_plain",
    ".jpg": "screenshot", ".jpeg": "screenshot", ".png": "screenshot",
    ".gif": "screenshot", ".bmp": "screenshot", ".webp": "screenshot",
    ".zip": "archive", ".rar": "archive", ".7z": "archive", ".tar": "archive",
    ".gz": "archive",
    ".jar": "jar", ".exe": "exe", ".msi": "exe", ".dll": "exe",
}

# Magic-byte signatures -> (subtype, is_zip_like).
_MAGIC = [
    (b"%PDF", "pdf", False),
    (b"\xff\xd8\xff", "screenshot", False),  # JPEG
    (b"\x89PNG", "screenshot", False),        # PNG
    (b"#!SILK", "voice", False),              # WeChat voice (silk audio)
    (b"PK", None, True),                      # zip container -> inspect further
    (b"MZ", "exe", False),                    # PE/exe/dll
    (b"Rar!", "archive", False),
    (b"7z", "archive", False),
]


def _sniff_zip(path: Path) -> str:
    """Inside a PK zip, distinguish docx/xlsx/jar vs plain zip by inspecting
    the central directory names (cheap: read first entries via zipfile)."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return "unknown"
    joined = "|".join(names[:200])
    if "word/" in joined and "[Content_Types].xml" in joined:
        return "docx"
    if "xl/" in joined and "[Content_Types].xml" in joined:
        return "xlsx"
    if "META-INF/MANIFEST.MF" in joined:
        return "jar"
    return "archive"


def _looks_like_text(path: Path) -> bool:
    """Heuristic for extension-less plain text: decodable as UTF-8 and
    mostly printable (WeChat sometimes strips .txt on transfer)."""
    try:
        raw = path.read_bytes()[:4096]
        if not raw:
            return False
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if any(c in text for c in "\x00\x01\x02"):
        return False  # control bytes → binary
    printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
    return printable / len(text) > 0.9


def detect_type(path: Path) -> str:
    """Return the level-2 subtype for a file, or 'unknown'."""
    magic_hit = None
    try:
        with open(path, "rb") as f:
            head = f.read(8)  # longest signature is 6 bytes (#!SILK); read 8
    except OSError:
        return "unknown"

    for sig, subtype, _zip_like in _MAGIC:
        if head.startswith(sig):
            magic_hit = subtype
            if sig == b"PK":
                return _sniff_zip(path)
            break

    if magic_hit:
        return magic_hit

    # Fall back to extension.
    ext = path.suffix.lower()
    subtype = _EXT_MAP.get(ext)
    if subtype:
        return subtype

    # Extension-less or unrecognized: plain-text heuristic (txt without
    # extension is common on IM transfers).
    if _looks_like_text(path):
        return "text_plain"
    return "unknown"
