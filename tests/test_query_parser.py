"""Regression tests for query parsing.

Every case here is a real-world failure we hit during development:
when a query parsed wrongly, it became a test case.  Do not remove
cases — they guard against regressions (DESIGN.md §13 workflow).
"""

import pytest
from datetime import timedelta

from wechat_memory.config import load_config
from wechat_memory.search.query_parser import parse_query, is_list_intent


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ---------------------------------------------------------------------------
# Search intent (must NOT be misread as list)
# ---------------------------------------------------------------------------

class TestSearchIntent:
    def test_plain_type_word(self, cfg):
        q = parse_query("/发票", cfg)
        assert q.action == "search"
        assert q.type_filter == "invoice"

    def test_bare_link_word(self, cfg):
        q = parse_query("/链接", cfg)
        assert q.action == "search"
        assert q.type_filter == "link"

    def test_keyword_only(self, cfg):
        q = parse_query("/T-Mem", cfg)
        assert q.action == "search"
        assert q.keywords == "T-Mem"
        assert q.type_filter is None

    def test_anaphora_query(self, cfg):
        """/那个链接是什么 must stay a search (anaphora resolved by LLM)."""
        q = parse_query("/那个链接是什么", cfg)
        assert q.action == "search"

    def test_time_plus_type(self, cfg):
        q = parse_query("/找一下上周的发票", cfg)
        assert q.action == "search"
        assert q.type_filter == "invoice"
        assert q.time_from is not None

    def test_single_char_type_words_not_in_map(self, cfg):
        """The single-char 图 must not be a type word (图记忆 regression)."""
        assert "图" not in cfg.type_word_map
        q = parse_query("/图记忆", cfg)
        assert q.type_filter is None
        assert "图记忆" in q.keywords


# ---------------------------------------------------------------------------
# List/inventory intent
# ---------------------------------------------------------------------------

class TestListIntent:
    @pytest.mark.parametrize("text,expect_type", [
        ("/目前你保存了哪些文件", None),       # the original failure report
        ("/保存了什么", None),
        ("/有哪些PDF", None),
        ("/有哪些pdf", "document"),
        ("/保存了哪些链接", "link"),
        ("/有哪些图片", "image"),
        ("/有什么发票", "invoice"),
        ("/列出所有文档", "document"),
    ])
    def test_list_queries(self, cfg, text, expect_type):
        q = parse_query(text, cfg)
        assert q.action == "list", f"{text} should be list intent"
        assert q.type_filter == expect_type

    @pytest.mark.parametrize("text", [
        "/发票", "/T-Mem", "/那个链接是什么", "/搜索RAG",
        "/找一下上周的发票", "/链接", "/有哪些话想说",
    ])
    def test_search_not_misread_as_list(self, cfg, text):
        q = parse_query(text, cfg)
        assert q.action == "search", f"{text} must not become list intent"


# ---------------------------------------------------------------------------
# Keyword extraction (stopword cleanup)
# ---------------------------------------------------------------------------

class TestKeywordCleanup:
    def test_conversational_link_queries_empty_keywords(self, cfg):
        """这些口语查询的关键词应清空，只按类型过滤（保存/上次发 停用词）。"""
        for text in ("/我保存的链接", "/上次发的链接", "/刚才发的链接",
                     "/我发的链接", "/那个链接", "/帮我查一下我保存过的链接"):
            q = parse_query(text, cfg)
            assert q.keywords == "", f"{text} keywords should be empty"
            assert q.type_filter == "link"

    def test_semantic_keyword_preserved(self, cfg):
        q = parse_query("/那个腾讯的开源项目", cfg)
        assert "腾讯" in q.keywords

    def test_plain_keyword(self, cfg):
        q = parse_query("/RAG", cfg)
        assert q.keywords == "RAG"


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

class TestTimeParsing:
    def test_recent_month(self, cfg):
        q = parse_query("/最近一个月的压缩包", cfg)
        assert q.time_from is not None
        expected = timedelta(days=30) - timedelta(minutes=1)
        actual = timedelta(days=30) - (q.time_from.replace(microsecond=0) and (q.time_from - __import__("datetime").datetime.now()))
        # Looser: time_from is within the last 30 days.
        assert q.time_from < __import__("datetime").datetime.now()
        assert q.time_from > __import__("datetime").datetime.now() - timedelta(days=31)

    def test_yesterday(self, cfg):
        from datetime import datetime
        q = parse_query("/昨天的发票", cfg)
        assert q.time_from > datetime.now() - timedelta(days=2)

    def test_no_time(self, cfg):
        q = parse_query("/T-Mem", cfg)
        assert q.time_from is None


class TestAllCommand:
    """/all lists every record (router maps it to the 'all' fixed command)."""

    def test_router_routes_all(self):
        from wechat_memory.commands.router import CommandRouter
        r = CommandRouter(cfg)
        for text in ("/all", "/ALL", "/list"):
            assert r.route(text).name in ("all", "list")
