"""Regression tests for search (LIKE + FTS5 + filters) on a temp database.

Every case mirrors a real search failure or requirement observed during
development: conversational queries, type filtering, CJK matching.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wechat_memory.config import load_config
from wechat_memory.search.query_parser import parse_query
from wechat_memory.search.searcher import Searcher
from wechat_memory.storage.db import connect


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """A temp index.db seeded with records mirroring real usage."""
    cfg = load_config()
    db_path = tmp_path_factory.mktemp("search") / "index.db"
    conn = connect(db_path)
    now = datetime.now()

    def seed(msg_id, mtype, text, subtype, ext_text, title, summary, keywords,
             days_ago, url=""):
        import json as _json
        ts = (now - timedelta(days=days_ago)).isoformat()
        conn.execute(
            "INSERT INTO messages (msg_id, timestamp, session, sender_id,"
            " message_type, raw_dir, text, raw_json) VALUES (?,?,?,?,?,?,?,?)",
            (msg_id, ts, "me", "me", mtype, f"/raw/{msg_id}", text, "{}"),
        )
        cur = conn.execute(
            "INSERT INTO attachments (msg_id, subtype, filename, extracted_text)"
            " VALUES (?,?,?,?)",
            (msg_id, subtype, f"{msg_id}.bin", ext_text),
        )
        conn.execute(
            "INSERT INTO documents (msg_id, attach_id, summary, keywords,"
            " category, title) VALUES (?,?,?,?,?,?)",
            (msg_id, cur.lastrowid, summary, _json.dumps(keywords, ensure_ascii=False),
             "", title),
        )
        if url:
            conn.execute(
                "INSERT INTO links (msg_id, attach_id, url, fetch_status)"
                " VALUES (?,?,?,'ok')",
                (msg_id, cur.lastrowid, url),
            )

    seed("m_invoice", "invoice", "", "pdf", "电子发票 餐费 151元",
         "餐费电子发票", "火锅店餐费151元", ["发票", "餐费"], 30)
    seed("m_link", "link", "", "link", "T-Mem memory EMNLP",
         "T-Mem：触发增强图记忆系统", "EMNLP 2026 论文", ["记忆", "EMNLP"], 2,
         url="https://github.com/Sherlockwz/T-Mem")
    seed("m_image", "image", "", "screenshot", "BOSS直聘 AI应用全栈工程师 招聘",
         "AI工程师职位截图", "招聘信息截图", ["招聘", "BOSS直聘"], 20)
    seed("m_doc", "document", "", "pdf", "RAG 检索增强生成 实践指南",
         "RAG实践指南", "关于检索增强的文档", ["RAG", "检索"], 15)
    conn.commit()
    yield conn
    conn.close()


def run(db, text, cfg):
    q = parse_query(text, cfg)
    return q, Searcher(db).search(q)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


class TestCJKKeywordSearch:
    """FTS5 tokenizes CJK poorly — LIKE on metadata is the primary channel."""

    def test_single_cjk_word_hits_summary(self, db, cfg):
        _, hits = run(db, "/记忆", cfg)
        assert any(h.msg_id == "m_link" for h in hits)

    def test_title_word(self, db, cfg):
        _, hits = run(db, "/RAG", cfg)
        assert any(h.msg_id == "m_doc" for h in hits)

    def test_fulltext_fallback(self, db, cfg):
        """/EMNLP only exists in link's extracted text — must still hit."""
        _, hits = run(db, "/EMNLP", cfg)
        assert any(h.msg_id == "m_link" for h in hits)


class TestConversationalQueries:
    """These all failed at some point during development."""

    def test_my_saved_link(self, db, cfg):
        q, hits = run(db, "/我保存的链接", cfg)
        assert q.type_filter == "link"
        assert any(h.msg_id == "m_link" for h in hits)

    def test_last_time_link(self, db, cfg):
        _, hits = run(db, "/上次发的链接", cfg)
        assert any(h.msg_id == "m_link" for h in hits)

    def test_that_link(self, db, cfg):
        _, hits = run(db, "/那个链接", cfg)
        assert any(h.msg_id == "m_link" for h in hits)


class TestTypeAndTimeFilters:
    def test_type_filter_only(self, db, cfg):
        _, hits = run(db, "/发票", cfg)
        assert [h.msg_id for h in hits] == ["m_invoice"]

    def test_type_plus_time(self, db, cfg):
        q = parse_query("/上周的发票", cfg)
        hits = Searcher(db).search(q)
        # m_invoice is 30 days old — outside "上周".
        assert hits == []

    def test_list_action_lists_recent(self, db, cfg):
        q = parse_query("/目前保存了哪些文件", cfg)
        assert q.action == "list"
        hits = Searcher(db).search(q, limit=20)
        assert len(hits) == 4   # all seeds, most recent first

    def test_list_with_type_filter(self, db, cfg):
        q = parse_query("/保存了哪些链接", cfg)
        assert q.action == "list"
        assert q.type_filter == "link"
        hits = Searcher(db).search(q, limit=20)
        assert [h.msg_id for h in hits] == ["m_link"]


class TestNoResults:
    def test_honest_empty(self, db, cfg):
        q, hits = run(db, "/不存在的主题xyz", cfg)
        assert hits == []
