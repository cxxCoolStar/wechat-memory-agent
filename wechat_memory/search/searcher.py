"""Search execution (DESIGN.md §12): metadata FTS5 as primary channel,
extracted-fulltext FTS5 as fallback, structured filters as conditions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from .query_parser import ParsedQuery


@dataclass
class SearchHit:
    """One search result, rendered as a card in the reply."""

    msg_id: str
    timestamp: datetime
    message_type: str
    title: str
    summary: str
    match_reason: str
    raw_dir: str


class Searcher:
    """Runs a ParsedQuery against the SQLite index."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def search(self, query: ParsedQuery, limit: int = 8) -> List[SearchHit]:
        """Execute the query.  Returns up to ``limit`` hits sorted by
        relevance (metadata match first, then fulltext, then time)."""
        # TODO(impl): build the SQL combining:
        #   - type filter:  messages.message_type = ?
        #   - time filter:  messages.timestamp BETWEEN ? AND ?
        #   - metadata match: fts_metadata MATCH keywords (primary)
        #   - fulltext match:  fts_fulltext MATCH keywords (fallback)
        #   - rank: metadata hit > fulltext hit, then recency
        raise NotImplementedError

    def get_detail(self, msg_id: str) -> Optional[dict]:
        """Full detail for a chosen message (used by /<n> interaction)."""
        raise NotImplementedError
