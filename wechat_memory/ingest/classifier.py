"""Attachment classification (DESIGN.md §6).

Maps a detected subtype to a level-1 type, and decides whether an image is
worth OCR'ing (screenshot-like) or is a photo (metadata only).
"""

from __future__ import annotations

from ..models import (
    BINARY, BIN_ARCHIVE, BIN_EXE, BIN_JAR, BIN_UNKNOWN,
    DOCUMENT, DOC_DOCX, DOC_PDF, DOC_XLSX,
    IMAGE, IMG_PHOTO, IMG_SCREENSHOT, INVOICE, LINK, OTHER, TEXT,
)

# subtype -> level-1 type
_SUBTYPE_TO_LEVEL1 = {
    DOC_PDF: DOCUMENT,
    DOC_DOCX: DOCUMENT,
    DOC_XLSX: DOCUMENT,
    IMG_SCREENSHOT: IMAGE,
    IMG_PHOTO: IMAGE,
    BIN_JAR: BINARY,
    BIN_EXE: BINARY,
    BIN_ARCHIVE: BINARY,
    BIN_UNKNOWN: BINARY,
}


def subtype_to_level1(subtype: str) -> str:
    """Map a detected subtype to its level-1 type.  'unknown' -> OTHER."""
    return _SUBTYPE_TO_LEVEL1.get(subtype, OTHER)


def should_ocr(subtype: str) -> bool:
    """Whether an image attachment should go through OCR."""
    return subtype == IMG_SCREENSHOT


def is_invoice_candidate(filename: str, extracted_text: str) -> bool:
    """Heuristic: filename contains 发票/invoice, or text mentions 发票."""
    if "发票" in filename or "invoice" in filename.lower():
        return True
    return "电子发票" in extracted_text or "发票" in extracted_text[:200]
