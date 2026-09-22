"""Natural-language query parsing: rule-based first, LLM fallback
(DESIGN.md §10.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..config import Config


@dataclass
class ParsedQuery:
    """Structured query extracted from a natural-language command."""

    keywords: str = ""                # remaining text for FTS5
    type_filter: Optional[str] = None  # level-1 type, e.g. "document"
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None
    raw_text: str = ""
    parsed_by_llm: bool = False       # True when produced by the LLM fallback

    def describe(self) -> str:
        parts = []
        if self.type_filter:
            parts.append(f"类型={self.type_filter}")
        if self.time_from:
            parts.append(f"时间从 {self.time_from:%Y-%m-%d}")
        if self.keywords:
            parts.append(f"关键词='{self.keywords}'")
        return ", ".join(parts) or "全部记录"


# --- Time-word rules -------------------------------------------------------

_TIME_RULES = [
    # (regex, timedelta offset for start)
    (r"最近|近\s*\d*\s*天", lambda: timedelta(days=30)),
    (r"最近一个月|近一个月|一个月(内|以)", lambda: timedelta(days=30)),
    (r"本周|这周", lambda: timedelta(days=7)),
    (r"上周", lambda: timedelta(days=14)),
    (r"本月|这个月", lambda: timedelta(days=30)),
    (r"上个月|上月", lambda: timedelta(days=60)),
    (r"昨天|昨日", lambda: timedelta(days=1)),
]


def _parse_time(text: str) -> Optional[timedelta]:
    for pattern, factory in _TIME_RULES:
        if re.search(pattern, text):
            return factory()
    return None


# --- Type-word mapping -----------------------------------------------------

def _parse_type(text: str, type_word_map: dict) -> Optional[str]:
    for word, typ in type_word_map.items():
        if word in text:
            return typ
    return None


def parse_query(raw_text: str, cfg: Config) -> ParsedQuery:
    """Rule-based parse.  Returns a ParsedQuery; caller may fall back to LLM
    when this yields nothing useful."""
    q = ParsedQuery(raw_text=raw_text)

    # Time.
    delta = _parse_time(raw_text)
    if delta:
        q.time_from = datetime.now() - delta

    # Type.
    q.type_filter = _parse_type(raw_text, cfg.type_word_map)

    # Keywords: strip common framing words, keep the rest.
    cleaned = re.sub(r"^[/\s]*", "", raw_text)
    # 框架词/语气词
    cleaned = re.sub(r"(帮我|帮我查|查一下|找一下|搜索一下|搜一下|找找|看看|查询)(的)?", "", cleaned)
    cleaned = re.sub(r"(我|你|他|她)(上传|发的|发过|保存|存过|保存过)?(的|了)?", "", cleaned)
    # 时间短语（含"刚才/上次"这类相对时间）
    cleaned = re.sub(r"(最近|最近一个月|近一个月|上周|上个月|昨天|本月|本周|刚才|上次|之前|以前|一个月内|一个月)", "", cleaned)
    # 量词/助词/语气词
    cleaned = re.sub(r"(一个|这个|那个|一下|的|了|一条|那个|有没有)", "", cleaned)
    # Remove type words that were consumed.
    for word in cfg.type_word_map:
        cleaned = cleaned.replace(word, "")
    cleaned = cleaned.strip(" ，。、")
    # If nothing meaningful remains, treat as "no keyword" (type/time-only search).
    if cleaned in {"", "发", "找", "查", "看", "有", "上"} or len(cleaned) <= 1:
        cleaned = ""
    q.keywords = cleaned
    return q
