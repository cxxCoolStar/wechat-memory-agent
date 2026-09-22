"""SQLite storage: schema and connection helpers.

Schema follows DESIGN.md §5.2.  FTS5 tables are created here; the actual
indexing logic lives in search/indexer.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
-- One row per WeChat message (the storage unit).
CREATE TABLE IF NOT EXISTS messages (
    msg_id        TEXT PRIMARY KEY,
    timestamp     TEXT NOT NULL,            -- ISO-8601
    session       TEXT NOT NULL,
    sender_id     TEXT NOT NULL,
    message_type  TEXT NOT NULL,            -- level-1 type
    raw_dir       TEXT NOT NULL,
    text          TEXT NOT NULL DEFAULT '', -- extracted text content
    raw_json      TEXT NOT NULL DEFAULT ''  -- original WeChat message structure
);

-- One row per attachment inside a message.
CREATE TABLE IF NOT EXISTS attachments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id         TEXT NOT NULL REFERENCES messages(msg_id),
    subtype        TEXT NOT NULL,           -- level-2 subtype
    filename       TEXT NOT NULL,
    original_name  TEXT NOT NULL DEFAULT '',
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    file_hash      TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL DEFAULT '',
    extracted_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(msg_id);

-- LLM-generated metadata (DESIGN.md §9.3). One row per attachment.
CREATE TABLE IF NOT EXISTS documents (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id    TEXT NOT NULL REFERENCES messages(msg_id),
    attach_id INTEGER NOT NULL REFERENCES attachments(id),
    summary   TEXT NOT NULL DEFAULT '',
    keywords  TEXT NOT NULL DEFAULT '[]',   -- JSON list
    category  TEXT NOT NULL DEFAULT '',
    entities  TEXT NOT NULL DEFAULT '[]',   -- JSON list
    title     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_documents_msg ON documents(msg_id);

-- Structured invoice fields (DESIGN.md §5.2).
CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id       TEXT NOT NULL REFERENCES messages(msg_id),
    invoice_no   TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    amount       REAL NOT NULL DEFAULT 0,
    seller       TEXT NOT NULL DEFAULT '',
    buyer        TEXT NOT NULL DEFAULT '',
    tax_no       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_invoices_msg ON invoices(msg_id);

-- Link snapshots (DESIGN.md §7).
CREATE TABLE IF NOT EXISTS links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id       TEXT NOT NULL REFERENCES messages(msg_id),
    attach_id    INTEGER NOT NULL REFERENCES attachments(id),
    url          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    snapshot_path TEXT NOT NULL DEFAULT '',
    fetch_status TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_links_msg ON links(msg_id);

-- FTS5: metadata (summary/keywords/category/title).
CREATE VIRTUAL TABLE IF NOT EXISTS fts_metadata USING fts5(
    msg_id UNINDEXED, title, summary, category, keywords
);

-- FTS5: extracted full text (fallback search).
CREATE VIRTUAL TABLE IF NOT EXISTS fts_fulltext USING fts5(
    msg_id UNINDEXED, body
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn
