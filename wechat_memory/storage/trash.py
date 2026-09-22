"""Soft-delete support (DESIGN.md deletion design).

Delete = move raw dir to raw/.trash/, snapshot all DB rows into the trash
table, then remove rows + FTS entries.  Undo restores everything from the
snapshot.  Entries older than TRASH_RETENTION_DAYS are purged at startup.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from ..search.indexer import Indexer

logger = logging.getLogger(__name__)

TRASH_RETENTION_DAYS = 7

# Tables holding per-message rows.  Delete order: documents/invoices/links
# reference attachments(attach_id), which references messages — so leaf
# tables first, then attachments, then messages.  Restore order is the
# exact reverse.
_DATA_TABLES = ["documents", "invoices", "links", "attachments", "messages"]
_RESTORE_TABLES = ["messages", "attachments", "documents", "invoices", "links"]
_FTS_TABLES = ["fts_metadata", "fts_fulltext"]


class TrashManager:
    """Soft delete / undo / purge for messages."""

    def __init__(self, cfg, conn):
        self._cfg = cfg
        self._conn = conn
        self._trash_root = cfg.raw_dir / ".trash"

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------
    def soft_delete(self, msg_id: str) -> bool:
        """Move a message to the trash.  Returns False if not found."""
        conn = self._conn
        row = conn.execute(
            "SELECT msg_id FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if not row:
            return False

        # 1. Snapshot every row for this message (delete order = child first).
        snapshot = {}
        for table in _DATA_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE msg_id = ?", (msg_id,)
            ).fetchall()
            snapshot[table] = [dict(r) for r in rows]

        # 2. Move the raw dir into .trash/.
        src = Path(cfg_raw_dir(self._cfg)) / msg_id
        if src.exists():
            self._trash_root.mkdir(parents=True, exist_ok=True)
            dst = self._trash_root / msg_id
            if dst.exists():  # stale leftovers from an earlier cycle
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))

        # 3. Record in trash table.
        conn.execute(
            "INSERT OR REPLACE INTO trash (msg_id, deleted_at, snapshot)"
            " VALUES (?,?,?)",
            (msg_id, datetime.now().isoformat(), json.dumps(snapshot,
                                                            ensure_ascii=False)),
        )

        # 4. Remove live rows + FTS.
        for table in _FTS_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE msg_id = ?", (msg_id,))
        for table in _DATA_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE msg_id = ?", (msg_id,))
        conn.commit()
        logger.info("soft-deleted %s", msg_id)
        return True

    # ------------------------------------------------------------------
    # undo
    # ------------------------------------------------------------------
    def undo_last(self) -> Optional[str]:
        """Restore the most recently deleted message.  Returns msg_id."""
        row = self._conn.execute(
            "SELECT id, msg_id, snapshot FROM trash"
            " ORDER BY deleted_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        restored = self._restore(row["msg_id"], row["snapshot"])
        if restored:
            self._conn.execute("DELETE FROM trash WHERE id = ?", (row["id"],))
            self._conn.commit()
        return row["msg_id"] if restored else None

    def undo_by_index(self, index: int, listing: List[dict]) -> Optional[str]:
        """Restore entry #index (1-based) from ``listing`` (see list_entries)."""
        if index < 1 or index > len(listing):
            return None
        msg_id = listing[index - 1]["msg_id"]
        row = self._conn.execute(
            "SELECT snapshot FROM trash WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if not row:
            return None
        restored = self._restore(msg_id, row["snapshot"])
        if restored:
            self._conn.execute("DELETE FROM trash WHERE msg_id = ?", (msg_id,))
            self._conn.commit()
        return msg_id if restored else None

    def _restore(self, msg_id: str, snapshot_json: str) -> bool:
        conn = self._conn
        # Already alive?  Nothing to do.
        if conn.execute("SELECT 1 FROM messages WHERE msg_id = ?",
                        (msg_id,)).fetchone():
            return False
        snapshot = json.loads(snapshot_json)
        for table in _RESTORE_TABLES:  # parents before children
            rows = snapshot.get(table) or []
            for r in rows:
                cols = ", ".join(r.keys())
                marks = ", ".join("?" for _ in r)
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})",
                    tuple(r.values()),
                )
        # Move the raw dir back.
        trash_dir = self._trash_root / msg_id
        if trash_dir.exists():
            live = Path(cfg_raw_dir(self._cfg)) / msg_id
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trash_dir), str(live))
        # Rebuild FTS.
        Indexer(conn).index_message(msg_id)
        conn.commit()
        logger.info("restored %s", msg_id)
        return True

    # ------------------------------------------------------------------
    # listing / purge
    # ------------------------------------------------------------------
    def list_entries(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT msg_id, deleted_at, snapshot FROM trash"
            " ORDER BY deleted_at DESC"
        ).fetchall()
        entries = []
        for r in rows:
            # Pull the title out of the stored snapshot (live documents
            # rows are already deleted).
            title = ""
            try:
                docs = json.loads(r["snapshot"]).get("documents") or []
                if docs:
                    title = docs[0].get("title") or ""
            except (json.JSONDecodeError, AttributeError, IndexError):
                pass
            entries.append({"msg_id": r["msg_id"],
                            "deleted_at": r["deleted_at"],
                            "title": title})
        return entries

    def purge_expired(self) -> int:
        """Permanently delete trash entries older than the retention window."""
        cutoff = (datetime.now() - timedelta(days=TRASH_RETENTION_DAYS)).isoformat()
        rows = self._conn.execute(
            "SELECT msg_id FROM trash WHERE deleted_at < ?", (cutoff,)
        ).fetchall()
        for r in rows:
            d = self._trash_root / r["msg_id"]
            if d.exists():
                shutil.rmtree(d)
            self._conn.execute("DELETE FROM trash WHERE msg_id = ?", (r["msg_id"],))
        if rows:
            self._conn.commit()
            logger.info("purged %d expired trash entries", len(rows))
        return len(rows)


def cfg_raw_dir(cfg):
    """Path helper: the live raw directory."""
    return cfg.raw_dir
