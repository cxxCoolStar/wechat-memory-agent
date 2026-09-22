"""Core data models shared across the whole project.

These mirror the logical storage design in DESIGN.md:
- one ``Message`` per WeChat message (the storage unit),
- zero or more ``Attachment`` per message,
- each attachment may carry an ``ItemMetadata`` (LLM-generated).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Enums / type constants
# ---------------------------------------------------------------------------

# Level-1 types (retrieval filtering).  One of these per message.
TEXT = "text"
DOCUMENT = "document"
INVOICE = "invoice"
IMAGE = "image"
LINK = "link"
BINARY = "binary"
OTHER = "other"

# Level-2 subtypes (pipeline selection).  Stored on each attachment.
DOC_PDF = "pdf"
DOC_DOCX = "docx"
DOC_XLSX = "xlsx"
IMG_SCREENSHOT = "screenshot"
IMG_PHOTO = "photo"
BIN_JAR = "jar"
BIN_EXE = "exe"
BIN_ARCHIVE = "archive"
BIN_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def gen_msg_id(raw_message: Dict[str, Any], fallback_ts: Optional[datetime] = None) -> str:
    """Generate a stable, reproducible message id.

    See DESIGN.md §4: ``<ts>-<sha256 fragment>`` so the id is readable
    (time) and unique (content fingerprint), independent of the platform's
    own message_id stability.
    """
    source_id = str(raw_message.get("message_id") or "")
    sender = str(raw_message.get("from_user_id") or "")
    content = json.dumps(raw_message.get("item_list"), ensure_ascii=False)
    ts = fallback_ts or datetime.now()
    digest = hashlib.sha256(f"{source_id}|{sender}|{content}".encode()).hexdigest()[:12]
    return f"{ts:%Y%m%d%H%M%S}-{digest}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    """A single file/attachment inside a message (image, document, link...)."""

    subtype: str                        # level-2 subtype, e.g. "pdf", "screenshot"
    filename: str                       # stored filename inside the raw dir
    original_name: str = ""             # user-visible name (may differ)
    size_bytes: int = 0
    file_hash: str = ""
    # For extracted text: reference into extracted/ (path) or inline text.
    extracted_text: str = ""
    extracted_path: str = ""
    # LLM-generated metadata for this attachment (see ItemMetadata).
    metadata: Optional["ItemMetadata"] = None

    # Link-specific fields (only for LINK attachments).
    url: str = ""
    title: str = ""
    description: str = ""
    link_snapshot_path: str = ""
    fetch_status: str = ""


@dataclass
class ItemMetadata:
    """LLM-generated metadata (DESIGN.md §9.3)."""

    summary: str = ""
    keywords: List[str] = field(default_factory=list)
    category: str = ""
    entities: List[str] = field(default_factory=list)
    title: str = ""

    def to_indexable_text(self) -> str:
        """Text that FTS5 indexes for this item."""
        parts = [self.title, self.summary, self.category, *self.keywords]
        return "\n".join(p for p in parts if p)


@dataclass
class InvoiceData:
    """Structured fields extracted from an invoice PDF (DESIGN.md §5.2)."""

    invoice_no: str = ""
    invoice_date: str = ""
    amount: float = 0.0
    seller: str = ""
    buyer: str = ""
    tax_no: str = ""


@dataclass
class Message:
    """One WeChat message = the storage unit (DESIGN.md §5)."""

    msg_id: str
    timestamp: datetime
    session: str                    # chat id
    sender_id: str
    message_type: str               # level-1 type
    raw_dir: str                    # path to this message's raw directory
    text: str = ""                  # extracted text content (if any)
    text_metadata: Optional["ItemMetadata"] = None  # LLM metadata for pure-text messages
    attachments: List[Attachment] = field(default_factory=list)
    invoice: Optional[InvoiceData] = None
    raw_message: Dict[str, Any] = field(default_factory=dict)  # original WeChat structure

    def attachment_count(self) -> int:
        return len(self.attachments)
