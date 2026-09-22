"""Search execution (DESIGN.md §12): metadata FTS5 as primary channel,
extracted-fulltext FTS5 as fallback, structured filters as conditions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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

    def search(self, query: ParsedQuery, limit: int = 8,
               expand_terms: Optional[List[str]] = None) -> List[SearchHit]:
        """Execute the query.  Returns up to ``limit`` hits.

        ``expand_terms``: alias terms confirmed via knowledge consolidation;
        each broadens the metadata match (DESIGN.md daily-merge design).

        Strategy:
          1. Metadata FTS5 match (primary) — rank 0.
          2. Fulltext FTS5 match (fallback)  — rank 1.
          3. No keyword: list by recency within type/time filters.
        """
        where = []
        params: list = []

        if query.type_filter:
            where.append("m.message_type = ?")
            params.append(query.type_filter)
        if query.time_from:
            where.append("m.timestamp >= ?")
            params.append(query.time_from.isoformat())
        if query.time_to:
            where.append("m.timestamp <= ?")
            params.append(query.time_to.isoformat())

        # Keyword search: LIKE on metadata + fulltext is the primary channel
        # for Chinese (FTS5 tokenizes poorly on CJK); FTS5 MATCH is a bonus.
        if query.keywords:
            like = f"%{query.keywords}%"
            expand = expand_terms or []
            # Alias expansion: each confirmed alias term gets its own LIKE arm.
            expand_clauses = "".join(
                " OR (d.title LIKE ? OR d.summary LIKE ? OR d.category LIKE ?)"
                for _ in expand
            )
            expand_params: list = []
            for term in expand:
                t = f"%{term}%"
                expand_params += [t, t, t]
            sql = (
                "SELECT m.msg_id, m.timestamp, m.message_type, m.raw_dir, "
                "       m.text, d.title, d.summary "
                "FROM messages m "
                "LEFT JOIN documents d ON d.msg_id = m.msg_id "
                "WHERE (" + (" AND ".join(where) if where else "1=1") + ") "
                "AND ("
                "  (d.title LIKE ? OR d.summary LIKE ? OR d.category LIKE ?) "
                "  OR m.text LIKE ? "
                "  OR EXISTS (SELECT 1 FROM attachments a WHERE a.msg_id = m.msg_id "
                "             AND a.extracted_text LIKE ?)"
                "  OR m.msg_id IN (SELECT msg_id FROM fts_metadata WHERE "
                "                  fts_metadata MATCH ?)"
                "  OR m.msg_id IN (SELECT msg_id FROM fts_fulltext WHERE "
                "                  fts_fulltext MATCH ?)"
                + expand_clauses +
                ") "
                "ORDER BY "
                "  CASE WHEN (d.title LIKE ? OR d.summary LIKE ? OR d.category LIKE ?) "
                "       THEN 0 ELSE 1 END, "
                "  m.timestamp DESC LIMIT ?"
            )
            fts_keyword = _fts_query(query.keywords)
            params2 = (
                params
                + [like, like, like, like, like, fts_keyword, fts_keyword]
                + expand_params
                + [like, like, like, limit]
            )
            rows = self._conn.execute(sql, params2).fetchall()
        else:
            sql = (
                "SELECT m.msg_id, m.timestamp, m.message_type, m.raw_dir, m.text, "
                "       d.title, d.summary "
                "FROM messages m LEFT JOIN documents d ON d.msg_id = m.msg_id "
                "WHERE " + (" AND ".join(where) if where else "1=1") + " "
                "ORDER BY m.timestamp DESC LIMIT ?"
            )
            rows = self._conn.execute(sql, params + [limit]).fetchall()

        hits = []
        for r in rows:
            hits.append(
                SearchHit(
                    msg_id=r["msg_id"],
                    timestamp=_parse_ts(r["timestamp"]),
                    message_type=r["message_type"],
                    title=r["title"] or "",
                    summary=r["summary"] or (r["text"] or "")[:80],
                    match_reason=_reason(query.keywords),
                    raw_dir=r["raw_dir"],
                )
            )
        return hits

    def get_detail(self, msg_id: str) -> Optional[dict]:
        """Full detail for a chosen message (used by /<n> interaction)."""
        m = self._conn.execute(
            "SELECT * FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if not m:
            return None
        atts = self._conn.execute(
            "SELECT * FROM attachments WHERE msg_id = ?", (msg_id,)
        ).fetchall()
        docs = self._conn.execute(
            "SELECT d.* FROM documents d JOIN attachments a ON d.attach_id = a.id "
            "WHERE d.msg_id = ?", (msg_id,)
        ).fetchall()
        inv = self._conn.execute(
            "SELECT * FROM invoices WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        return {
            "message": dict(m),
            "attachments": [dict(a) for a in atts],
            "documents": [dict(d) for d in docs],
            "invoice": dict(inv) if inv else None,
        }


def _fts_query(keywords: str) -> str:
    """Convert plain keywords into a safe FTS5 MATCH expression."""
    tokens = [k for k in keywords.split() if k]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def _parse_ts(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return datetime.now()


def _reason(keywords: str) -> str:
    if keywords:
        return f"关键词 '{keywords}' 命中"
    return "按类型/时间过滤"
