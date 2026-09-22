"""Index maintenance: writes into FTS5 tables (DESIGN.md §12.1).

A separate module from storage/db.py so indexing concerns stay isolated.
"""

from __future__ import annotations

import json
import sqlite3


class Indexer:
    """Populates fts_metadata and fts_fulltext for a stored message."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def index_message(self, msg_id: str) -> None:
        """Build/refresh FTS rows for one message.

        - fts_metadata: one row per attachment's documents.metadata.
        - fts_fulltext: one row per attachment's extracted_text + msg text.
        """
        conn = self._conn
        # Remove any existing rows first (idempotent reindex).
        self.remove_message(msg_id)

        # Metadata FTS: title/summary/category/keywords per attachment.
        rows = conn.execute(
            """SELECT d.title, d.summary, d.category, d.keywords
               FROM documents d JOIN attachments a ON d.attach_id = a.id
               WHERE d.msg_id = ?""",
            (msg_id,),
        ).fetchall()
        for row in rows:
            keywords = row["keywords"]
            if keywords:
                try:
                    keywords = " ".join(json.loads(keywords))
                except json.JSONDecodeError:
                    keywords = str(keywords)
            conn.execute(
                "INSERT INTO fts_metadata (msg_id, title, summary, category, keywords) "
                "VALUES (?,?,?,?,?)",
                (msg_id, row["title"], row["summary"], row["category"], keywords),
            )

        # Fulltext FTS: message text + each attachment's extracted text.
        body_parts = []
        m = conn.execute("SELECT text FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
        if m and m["text"]:
            body_parts.append(m["text"])
        att_rows = conn.execute(
            "SELECT extracted_text FROM attachments WHERE msg_id = ?", (msg_id,)
        ).fetchall()
        for a in att_rows:
            if a["extracted_text"]:
                body_parts.append(a["extracted_text"])
        body = "\n".join(body_parts)
        if body:
            conn.execute(
                "INSERT INTO fts_fulltext (msg_id, body) VALUES (?,?)", (msg_id, body)
            )

    def remove_message(self, msg_id: str) -> None:
        conn = self._conn
        conn.execute("DELETE FROM fts_metadata WHERE msg_id = ?", (msg_id,))
        conn.execute("DELETE FROM fts_fulltext WHERE msg_id = ?", (msg_id,))
