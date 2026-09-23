"""Ingestion pipeline orchestration (DESIGN.md §5, §6).

Turns an incoming Message into:
  1. raw/ storage (original files, untouched),
  2. extracted text per attachment,
  3. LLM-generated metadata,
  4. SQLite rows + FTS5 index.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from ..config import Config
from ..models import (
    BINARY, DOCUMENT, IMAGE, INVOICE, LINK, Message,
)
from ..search.indexer import Indexer
from ..utils.type_detect import detect_type
from .classifier import is_invoice_candidate, should_ocr, subtype_to_level1
from . import extractors
from .link_fetcher import LinkFetcher
from .llm_metadata import LLMMetadataGenerator

logger = logging.getLogger(__name__)


class IngestPipeline:
    """Coordinates all ingestion steps for one message."""

    def __init__(self, cfg: Config, conn):
        self._cfg = cfg
        self._conn = conn
        self._llm = LLMMetadataGenerator(cfg)
        self._links = LinkFetcher()
        self._indexer = Indexer(conn)

    # ------------------------------------------------------------------
    def ingest(self, message: Message) -> str:
        """Store one message and index it.  Returns the msg_id."""
        raw_dir = Path(message.raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)

        # 1. Persist the raw message structure + text.
        self._write_raw_meta(message, raw_dir)

        # 2. Process each attachment: classify -> extract -> metadata.
        for att in message.attachments:
            # Links carry no local file; handle them without a file check.
            if att.subtype == "link" or att.url:
                self._process_link(att, message)
                self._promote_type(message, LINK)
                continue
            src = raw_dir / att.filename
            if not src.exists():
                logger.warning("attachment missing: %s", src)
                continue

            # Detect real type (magic bytes > extension).
            subtype = detect_type(src)
            att.subtype = subtype
            level1 = subtype_to_level1(subtype)

            # Archive: try to extract text first; document if it yields
            # content, otherwise treat as binary (DESIGN.md §6.3).
            if level1 == DOCUMENT:
                self._process_document(att, src, message)
            elif level1 == IMAGE:
                self._process_image(att, src, message)
            elif level1 == LINK:
                self._process_link(att, message)
            elif level1 == BINARY and subtype == "archive":
                probe = extractors.extract_zip_text(src)
                if probe.strip():
                    att.extracted_text = probe
                    self._save_extracted(message.msg_id, att, "text")
                    att.metadata = self._llm.generate(probe, kind="document")
                    self._promote_type(message, DOCUMENT)
                else:
                    logger.info("archive %s has no readable text: binary", att.filename)
            elif level1 == BINARY:
                logger.info("binary %s: metadata only", att.filename)
            else:
                logger.info("unknown type %s: metadata only", att.filename)
            self._promote_type(message, level1)

        # Pure text messages (no attachments): generate metadata for the
        # message text itself so it is searchable by title/keywords.
        if not message.attachments and message.text.strip():
            try:
                meta = self._llm.generate(message.text, kind="text")
                if meta.summary or meta.keywords:
                    message.text_metadata = meta
            except Exception as exc:  # noqa: BLE001
                logger.warning("text metadata failed for %s: %s", message.msg_id, exc)

        # Determine message-level type: invoice if any attachment is invoice.
        if self._any_invoice(message):
            message.message_type = INVOICE

        # 3. Write SQLite rows.
        self._write_db(message)

        # 4. Index FTS5.
        self._indexer.index_message(message.msg_id)
        self._conn.commit()
        logger.info("ingested %s type=%s attachments=%d", message.msg_id,
                    message.message_type, len(message.attachments))
        return message.msg_id

    # ------------------------------------------------------------------
    def _write_raw_meta(self, message: Message, raw_dir: Path) -> None:
        meta = {
            "msg_id": message.msg_id,
            "timestamp": message.timestamp.isoformat(),
            "session": message.session,
            "sender_id": message.sender_id,
            "message_type": message.message_type,
            "text": message.text,
            "attachments": [
                {"filename": a.filename, "original_name": a.original_name}
                for a in message.attachments
            ],
            "raw_message": message.raw_message,
        }
        (raw_dir / "message.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if message.text:
            (raw_dir / "content.txt").write_text(message.text, encoding="utf-8")

    def _process_document(self, att, src: Path, message: Message) -> None:
        if att.subtype == "pdf":
            text = extractors.extract_pdf(src)
        elif att.subtype == "docx":
            text = extractors.extract_docx(src)
        elif att.subtype == "xlsx":
            text = extractors.extract_xlsx(src)
        elif att.subtype == "text_plain":
            # Plain text IS the content — no extraction needed, just a read.
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        elif att.subtype == "archive":
            text = extractors.extract_zip_text(src)
        else:
            text = ""
        att.extracted_text = text
        if text:
            if is_invoice_candidate(att.filename, text):
                try:
                    message.invoice = self._llm.generate_invoice(text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("invoice extraction failed: %s", exc)
                message.message_type = INVOICE
            att.metadata = self._llm.generate(text, kind="document")
        self._save_extracted(message.msg_id, att, "text")

    def _process_image(self, att, src: Path, message: Message) -> None:
        if should_ocr(att.subtype):
            try:
                text = extractors.ocr_image(src)
            except Exception as exc:  # noqa: BLE001
                logger.warning("OCR failed for %s: %s", src, exc)
                text = ""
            att.extracted_text = text
            if text:
                att.metadata = self._llm.generate(text, kind="image")
                self._save_extracted(message.msg_id, att, "ocr")
        # Photos without OCR text get no metadata (image text can't be sent
        # to the current text-only LLM).

    def _process_link(self, att, message: Message) -> None:
        snap = self._links.fetch(att.url)
        att.title = snap.title
        att.description = snap.description
        att.fetch_status = snap.status
        att.link_snapshot_path = snap.url  # keep url reference
        combined = "\n".join(
            p for p in (snap.title, snap.description, snap.body_excerpt) if p
        )
        if combined:
            att.extracted_text = combined
            att.metadata = self._llm.generate(combined, kind="link")

    def _promote_type(self, message: Message, level1: str) -> None:
        """Raise the message-level type to reflect its attachments.

        Priority (lowest -> highest): text < binary < image/link < document < invoice
        """
        order = {"text": 0, "binary": 1, "image": 2, "link": 2, "document": 3,
                 "invoice": 4, "other": 0}
        if order.get(level1, 0) > order.get(message.message_type, 0):
            message.message_type = level1

    def _save_extracted(self, msg_id: str, att, kind: str) -> None:
        """Write extracted text to extracted/<kind>/<msg_id>_<filename>.txt"""
        d = self._cfg.extracted_dir / kind
        d.mkdir(parents=True, exist_ok=True)
        safe = Path(att.filename).name
        out = d / f"{msg_id}_{safe}.txt"
        out.write_text(att.extracted_text, encoding="utf-8")
        att.extracted_path = str(out)

    def _any_invoice(self, message: Message) -> bool:
        return message.invoice is not None

    # ------------------------------------------------------------------
    def _write_db(self, message: Message) -> None:
        conn = self._conn
        conn.execute(
            """INSERT INTO messages (msg_id, timestamp, session, sender_id,
               message_type, raw_dir, text, raw_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                message.msg_id,
                message.timestamp.isoformat(),
                message.session,
                message.sender_id,
                message.message_type,
                str(message.raw_dir),
                message.text,
                json.dumps(message.raw_message, ensure_ascii=False),
            ),
        )

        # Pure-text messages: store the message-level metadata under a
        # synthetic attachment so documents/ link it consistently.
        if message.text_metadata and (message.text_metadata.summary
                                      or message.text_metadata.keywords):
            cur = conn.execute(
                """INSERT INTO attachments (msg_id, subtype, filename, original_name,
                   size_bytes, file_hash, extracted_text, extracted_path)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (message.msg_id, "text", "", "", 0, "", message.text, ""),
            )
            attach_id = cur.lastrowid
            md = message.text_metadata
            conn.execute(
                """INSERT INTO documents (msg_id, attach_id, summary, keywords,
                   category, entities, title) VALUES (?,?,?,?,?,?,?)""",
                (message.msg_id, attach_id, md.summary,
                 json.dumps(md.keywords, ensure_ascii=False), md.category,
                 json.dumps(md.entities, ensure_ascii=False), md.title),
            )

        for att in message.attachments:
            cur = conn.execute(
                """INSERT INTO attachments (msg_id, subtype, filename, original_name,
                   size_bytes, file_hash, extracted_text, extracted_path)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    message.msg_id, att.subtype, att.filename, att.original_name,
                    att.size_bytes, att.file_hash, att.extracted_text,
                    att.extracted_path,
                ),
            )
            attach_id = cur.lastrowid
            if att.metadata and (att.metadata.summary or att.metadata.keywords):
                conn.execute(
                    """INSERT INTO documents (msg_id, attach_id, summary, keywords,
                       category, entities, title) VALUES (?,?,?,?,?,?,?)""",
                    (
                        message.msg_id, attach_id, att.metadata.summary,
                        json.dumps(att.metadata.keywords, ensure_ascii=False),
                        att.metadata.category,
                        json.dumps(att.metadata.entities, ensure_ascii=False),
                        att.metadata.title,
                    ),
                )
            if att.url:
                conn.execute(
                    """INSERT INTO links (msg_id, attach_id, url, title, description,
                       snapshot_path, fetch_status) VALUES (?,?,?,?,?,?,?)""",
                    (
                        message.msg_id, attach_id, att.url, att.title,
                        att.description, att.link_snapshot_path, att.fetch_status,
                    ),
                )
        if message.invoice:
            inv = message.invoice
            conn.execute(
                """INSERT INTO invoices (msg_id, invoice_no, invoice_date, amount,
                   seller, buyer, tax_no) VALUES (?,?,?,?,?,?,?)""",
                (message.msg_id, inv.invoice_no, inv.invoice_date, inv.amount,
                 inv.seller, inv.buyer, inv.tax_no),
            )
