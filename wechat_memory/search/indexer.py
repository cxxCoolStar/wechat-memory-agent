"""Index maintenance: writes into FTS5 tables (DESIGN.md §12.1).

A separate module from storage/db.py so indexing concerns stay isolated.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


class Indexer:
    """Populates fts_metadata and fts_fulltext for a stored message."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def index_message(self, msg_id: str) -> None:
        """Build/refresh FTS rows for one message.

        - fts_metadata: one row joining documents.metadata for this msg.
        - fts_fulltext: one row joining attachments.extracted_text + messages.text.
        """
        # TODO(impl): INSERT INTO fts_metadata(msg_id, title, summary, category, keywords)
        #   SELECT ... FROM documents JOIN attachments WHERE msg_id = ?
        #   INSERT INTO fts_fulltext(msg_id, body)
        #   SELECT msg_id, text || extracted_text ... FROM messages JOIN attachments
        raise NotImplementedError

    def remove_message(self, msg_id: str) -> None:
        """Delete FTS rows for a removed message."""
        # TODO(impl): DELETE FROM fts_metadata WHERE msg_id = ?
        #   DELETE FROM fts_fulltext WHERE msg_id = ?
        raise NotImplementedError
