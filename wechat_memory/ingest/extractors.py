"""Per-type text extraction (DESIGN.md §6.4).

Each function returns extracted text for one attachment file; the pipeline
decides which to call based on the detected subtype.  All are pure functions
over file paths so they are easy to test.
"""

from __future__ import annotations

from pathlib import Path


def extract_pdf(path: Path) -> str:
    """Extract text from a PDF via PyMuPDF."""
    # TODO(impl): import fitz; iterate pages; return joined text
    raise NotImplementedError


def extract_docx(path: Path) -> str:
    """Extract paragraphs from a .docx via python-docx."""
    # TODO(impl)
    raise NotImplementedError


def extract_xlsx(path: Path) -> str:
    """Extract each sheet's cells via openpyxl, preserving row structure."""
    # TODO(impl)
    raise NotImplementedError


def extract_zip_text(path: Path) -> str:
    """Shallow-check a zip for readable docs (.md/.txt), return their text."""
    # TODO(impl): list entries, read small text files only
    raise NotImplementedError


def ocr_image(path: Path) -> str:
    """OCR an image via RapidOCR (used for screenshot-like images)."""
    # TODO(impl)
    raise NotImplementedError
