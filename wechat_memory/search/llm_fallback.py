"""LLM fallback for query parsing and zero-hit rewriting (DESIGN.md §10.2).

Two context blocks are loaded on demand (never resident):
- conversation history: the LAST round only (atomic queries, ~300 chars)
- knowledge-base snapshot: last 20 records' title+keywords+category (~2000 chars)

The fallback LLM call outputs either a direct match_id (snapshot pick) or a
rewritten query — never both required.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from ..config import Config
from ..utils.desensitize import desensitize
from .query_parser import ParsedQuery

logger = logging.getLogger(__name__)

# Pronouns / continuation words that force LLM parsing (rule parsing cannot
# resolve them without conversation context).
_ANAPHORA_WORDS = ("那个", "这个", "上次", "刚才", "还是", "再搜", "它", "上一个", "继续")

_TIMEOUT_SECONDS = 25  # Ark reasoning models often take >10s
_SNAPSHOT_LIMIT = 20
_SNAPSHOT_CHARS = 2000
_HISTORY_CHARS = 300

_PARSE_SYSTEM = """\
你是检索查询解析器。结合对话历史，把用户的当前查询转成 JSON，只输出 JSON：
{"action": "search|list", "keywords": "搜索关键词", "type": "text|document|invoice|image|link|binary|null", "time_range": "30d|7d|1d|last_month|null"}
规则：
- 用户想浏览/盘点已保存的内容（如"保存了哪些文件""有什么内容"）时 action 填 list
- 用户想找具体内容时 action 填 search
- 指代词（那个/上次/它等）要结合历史解析成具体内容
- 没有的字段填 null
- keywords 保留最有检索价值的词，去掉口语助词
"""

_REWRITE_SYSTEM = """\
你是检索兜底助手。用户的搜索词没有命中知识库。根据知识库现有条目，回答 JSON，只输出 JSON：
{"match_id": "最相关条目的msg_id，没有合适的填 null",
 "rewritten": {"keywords": "更可能命中的关键词", "type": "text|document|invoice|image|link|binary|null"}}
规则：
- 如果清单里有和用户意图明显相关的条目，直接给它的 msg_id
- 否则改写关键词（对齐清单里实际出现的词），match_id 填 null
"""


@dataclass
class HistoryRound:
    """One round of search conversation (the last round is all we keep)."""

    query_text: str = ""
    parsed_desc: str = ""          # ParsedQuery.describe()
    result_titles: List[str] = field(default_factory=list)

    def as_text(self) -> str:
        titles = "\n".join(f"  {t}" for t in self.result_titles[:3])
        return f"用户查: {self.query_text}\n解析: {self.parsed_desc}\n结果:\n{titles}"


class ConversationHistory:
    """In-memory, single-user, last-round-only conversation history."""

    def __init__(self) -> None:
        self._last: Optional[HistoryRound] = None

    def record(self, query_text: str, parsed_desc: str,
               result_titles: List[str]) -> None:
        self._last = HistoryRound(query_text, parsed_desc, result_titles)

    def peek(self) -> Optional[HistoryRound]:
        return self._last

    def as_prompt_block(self) -> str:
        r = self._last
        if not r:
            return "（无历史）"
        text = r.as_text()
        return text[:_HISTORY_CHARS]


def has_anaphora(text: str) -> bool:
    return any(w in text for w in _ANAPHORA_WORDS)


def build_kb_snapshot(conn) -> str:
    """Render the last N records' title/keywords/category as a short list."""
    rows = conn.execute(
        """SELECT d.msg_id, d.title, d.summary, d.category, d.keywords,
                  m.message_type
           FROM documents d JOIN messages m ON d.msg_id = m.msg_id
           ORDER BY d.id DESC LIMIT ?""",
        (_SNAPSHOT_LIMIT,),
    ).fetchall()
    if not rows:
        return "（知识库为空）"
    lines = []
    total = 0
    for r in rows:
        kw = r["keywords"]
        if kw:
            try:
                kw = " ".join(json.loads(kw))
            except json.JSONDecodeError:
                kw = str(kw)
        line = f"{r['msg_id']} | {r['message_type']} | {r['title']} | {kw}"
        total += len(line)
        if total > _SNAPSHOT_CHARS:
            break
        lines.append(line)
    return "\n".join(lines)


def _call_llm_json(cfg: Config, system: str, user: str) -> Optional[dict]:
    """One LLM call returning parsed JSON, or None on any failure."""
    if not cfg.llm_base_url or not cfg.llm_api_key:
        return None
    try:
        resp = requests.post(
            cfg.llm_base_url.rstrip("/") + "/chat/completions",
            json={
                "model": cfg.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 300,
            },
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - fallback must never raise
        logger.warning("LLM fallback call failed: %s", exc)
        return None

    t = content
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(t[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def llm_parse_query(cfg: Config, raw_text: str,
                    history: ConversationHistory) -> Optional[ParsedQuery]:
    """LLM-driven query parse with conversation history.  None on failure."""
    user = (
        f"[对话历史]\n{history.as_prompt_block()}\n\n"
        f"[当前查询]\n{desensitize(raw_text)}\n\n请输出 JSON。"
    )
    data = _call_llm_json(cfg, _PARSE_SYSTEM, user)
    if not data:
        return None
    q = ParsedQuery(
        keywords=str(data.get("keywords") or ""),
        raw_text=raw_text,
        parsed_by_llm=True,
    )
    if str(data.get("action") or "search") == "list":
        q.action = "list"
        q.keywords = ""
    typ = data.get("type")
    if typ in {"text", "document", "invoice", "image", "link", "binary"}:
        q.type_filter = typ
    tr = str(data.get("time_range") or "")
    from datetime import datetime, timedelta
    if tr == "1d":
        q.time_from = datetime.now() - timedelta(days=1)
    elif tr == "7d":
        q.time_from = datetime.now() - timedelta(days=7)
    elif tr == "30d" or tr == "last_month":
        q.time_from = datetime.now() - timedelta(days=30)
    return q


@dataclass
class RewriteResult:
    match_id: Optional[str] = None
    rewritten: Optional[ParsedQuery] = None


def llm_rewrite_zero_hit(cfg: Config, raw_query: str,
                         history: ConversationHistory,
                         snapshot: str) -> Optional[RewriteResult]:
    """Zero-hit fallback: prefer direct snapshot pick, else rewritten query."""
    user = (
        f"[知识库现有条目]\n{snapshot}\n\n"
        f"[对话历史]\n{history.as_prompt_block()}\n\n"
        f"[用户搜索词]\n{desensitize(raw_query)}\n\n请输出 JSON。"
    )
    data = _call_llm_json(cfg, _REWRITE_SYSTEM, user)
    if not data:
        return None
    result = RewriteResult(match_id=str(data.get("match_id") or "") or None)
    rw = data.get("rewritten") or {}
    if isinstance(rw, dict) and (rw.get("keywords") or rw.get("type")):
        q = ParsedQuery(keywords=str(rw.get("keywords") or ""), raw_text=raw_query,
                        parsed_by_llm=True)
        typ = rw.get("type")
        if typ in {"text", "document", "invoice", "image", "link", "binary"}:
            q.type_filter = typ
        result.rewritten = q
    if not result.match_id and not result.rewritten:
        return None
    return result
