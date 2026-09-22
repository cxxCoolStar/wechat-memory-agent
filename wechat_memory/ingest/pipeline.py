"""Ingestion pipeline orchestration (DESIGN.md §5, §6).

Turns an incoming Message into:
  1. raw/ storage (original files, untouched),
  2. extracted text per attachment,
  3. LLM-generated metadata,
  4. SQLite rows + FTS5 index.
"""

from __future__ import annotations

import sqlite3

from ..config import Config
from ..models import Message


class IngestPipeline:
    """Coordinates all ingestion steps for one message."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection):
        self._cfg = cfg
        self._conn = conn

    def ingest(self, message: Message) -> str:
        """Store one message and index it.  Returns the msg_id."""
        # 1. raw store: write message.json + attachment files into raw/<msg_id>/
        #    (raw_store module)                                       [TODO]
        # 2. classify each attachment via type_detect.detect_type      [TODO]
        # 3. extract text per type (PDF/Word/Excel/OCR/link)          [TODO]
        # 4. LLM metadata for attachments with text (desensitized)    [TODO]
        # 5. write SQLite rows (messages/attachments/documents/invoices/links)
        # 6. indexer.index_message(msg_id)
        raise NotImplementedError
