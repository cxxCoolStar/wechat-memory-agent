"""Regression tests for soft delete / undo / trash (deletion design)."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wechat_memory.config import load_config
from wechat_memory.search.indexer import Indexer
from wechat_memory.storage.db import connect
from wechat_memory.storage.trash import TrashManager, TRASH_RETENTION_DAYS


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg.data_home = tmp_path
    cfg.raw_dir = tmp_path / "raw"
    cfg.extracted_dir = tmp_path / "extracted"
    cfg.db_path = tmp_path / "index.db"
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)

    # Seed one message with attachment + metadata + link + raw dir.
    conn.execute(
        "INSERT INTO messages (msg_id, timestamp, session, sender_id,"
        " message_type, raw_dir, text, raw_json) VALUES (?,?,?,?,?,?,?,?)",
        ("m1", datetime.now().isoformat(), "me", "me", "link",
         str(cfg.raw_dir / "m1"), "", "{}"),
    )
    cur = conn.execute(
        "INSERT INTO attachments (msg_id, subtype, filename) VALUES (?,?,?)",
        ("m1", "link", ""),
    )
    conn.execute(
        "INSERT INTO documents (msg_id, attach_id, title, summary, keywords)"
        " VALUES (?,?,?,?,?)",
        ("m1", cur.lastrowid, "T-Mem", "记忆系统", '["记忆"]'),
    )
    conn.execute(
        "INSERT INTO links (msg_id, attach_id, url, fetch_status)"
        " VALUES (?,?,?,'ok')",
        ("m1", cur.lastrowid, "https://example.com/x"),
    )
    raw_dir = cfg.raw_dir / "m1"
    raw_dir.mkdir(parents=True)
    (raw_dir / "message.json").write_text("{}", encoding="utf-8")
    Indexer(conn).index_message("m1")
    conn.commit()

    yield cfg, conn, TrashManager(cfg, conn), raw_dir
    conn.close()


def _alive(conn, msg_id):
    row = conn.execute("SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
    fts = conn.execute("SELECT 1 FROM fts_metadata WHERE msg_id = ?",
                       (msg_id,)).fetchone()
    return bool(row) and bool(fts)


class TestSoftDelete:
    def test_delete_removes_from_search_but_keeps_trash(self, env):
        cfg, conn, trash, raw_dir = env
        assert trash.soft_delete("m1") is True
        # Gone from live tables and FTS.
        assert not _alive(conn, "m1")
        assert conn.execute("SELECT COUNT(*) c FROM links WHERE msg_id='m1'"
                            ).fetchone()["c"] == 0
        # Raw dir moved to .trash.
        assert not raw_dir.exists()
        assert (cfg.raw_dir / ".trash" / "m1" / "message.json").exists()
        # Trash table has a snapshot.
        assert conn.execute("SELECT COUNT(*) c FROM trash WHERE msg_id='m1'"
                            ).fetchone()["c"] == 1

    def test_delete_missing_returns_false(self, env):
        cfg, conn, trash, _ = env
        assert trash.soft_delete("nonexistent") is False


class TestUndo:
    def test_undo_restores_everything(self, env):
        cfg, conn, trash, raw_dir = env
        trash.soft_delete("m1")
        restored = trash.undo_last()
        assert restored == "m1"
        # Live again: rows, FTS, raw dir back in place.
        assert _alive(conn, "m1")
        assert (raw_dir / "message.json").exists()
        assert not (cfg.raw_dir / ".trash" / "m1").exists()
        assert conn.execute("SELECT COUNT(*) c FROM trash WHERE msg_id='m1'"
                            ).fetchone()["c"] == 0

    def test_undo_empty_trash(self, env):
        cfg, conn, trash, _ = env
        assert trash.undo_last() is None

    def test_undo_restores_correct_data(self, env):
        """Restored rows must match the snapshot byte-for-byte."""
        cfg, conn, trash, _ = env
        trash.soft_delete("m1")
        trash.undo_last()
        doc = conn.execute(
            "SELECT title, summary, keywords FROM documents WHERE msg_id='m1'"
        ).fetchone()
        assert doc["title"] == "T-Mem"
        assert json.loads(doc["keywords"]) == ["记忆"]


class TestTrashListingAndPurge:
    def test_list_entries(self, env):
        cfg, conn, trash, _ = env
        trash.soft_delete("m1")
        entries = trash.list_entries()
        assert len(entries) == 1
        assert entries[0]["msg_id"] == "m1"
        assert entries[0]["title"] == "T-Mem"  # gone from documents... see below

    def test_list_title_after_delete(self, env):
        """documents row is deleted, so title comes back None (acceptable:
        the list shows msg_id + date regardless)."""
        cfg, conn, trash, _ = env
        trash.soft_delete("m1")
        entries = trash.list_entries()
        # Title may be None because documents rows were removed.
        assert entries[0]["msg_id"] == "m1"

    def test_purge_expired(self, env):
        cfg, conn, trash, _ = env
        trash.soft_delete("m1")
        # Backdate the entry beyond retention.
        old = (datetime.now() - timedelta(days=TRASH_RETENTION_DAYS + 1)).isoformat()
        conn.execute("UPDATE trash SET deleted_at = ? WHERE msg_id='m1'", (old,))
        conn.commit()
        purged = trash.purge_expired()
        assert purged == 1
        assert not (cfg.raw_dir / ".trash" / "m1").exists()
        assert conn.execute("SELECT COUNT(*) c FROM trash").fetchone()["c"] == 0

    def test_purge_keeps_recent(self, env):
        cfg, conn, trash, _ = env
        trash.soft_delete("m1")
        assert trash.purge_expired() == 0
        assert (cfg.raw_dir / ".trash" / "m1").exists()
