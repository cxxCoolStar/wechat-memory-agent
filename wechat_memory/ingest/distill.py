"""Daily knowledge-consolidation engine (DESIGN.md daily-merge design).

Once a day the engine reads all LLM metadata, asks the LLM to find
merge candidates (entities/aliases/topic links) under the
"related ≠ same" rule, and pushes at most one proposal list per day to
the user.  Nothing is applied automatically — the user confirms via
/km N, which writes rows into knowledge_links.  Confirmed links enrich
retrieval (searcher expands queries via aliases/variants); raw data and
documents rows are never modified.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from ..config import Config
from ..utils.desensitize import desensitize

logger = logging.getLogger(__name__)

_PROPOSAL_SYSTEM = """\
你是个人知识库的整理助手。根据给定的知识条目元数据，找出可以归并的候选。
只输出 JSON：{"proposals": [...]}
每个提案：
{"kind": "duplicate"|"alias"|"link"|"version_of",
 "msg_ids": ["相关条目的msg_id", ...],       // duplicate/link 必填
 "canonical": "标准名",                       // alias 必填
 "variant": "别名",                           // alias 必填
 "newer_msg_id": "较新条目的msg_id",           // version_of 必填
 "older_msg_id": "较旧条目的msg_id",           // version_of 必填
 "reason": "一句话理由"}
规则（重要）：
- related ≠ same：内容相关不等于同指一物，宁可漏报不可错报
- 最多 5 条提案；没有可信候选就返回 {"proposals": []}
- alias：同一事物的不同叫法（如 T-Mem 与 触发增强图记忆）
- duplicate：完全无信息增量的重复（同文件重发、md5 相同）。
  ⚠️ 内容相似但有时间差/内容差的"同一文档的更新版"不属于此类，应报 version_of
- version_of：B 是 A 的更新版本（同一文档的不同时点快照，如资产统计的定期更新）。
  这类关系用于展示版本链，不合并、保留全部历史
- link：同一主题的强关联条目（如同一项目的多个材料）
"""


class DistillEngine:
    """Generates daily merge proposals and records confirmed links."""

    def __init__(self, cfg: Config, conn):
        self._cfg = cfg
        self._conn = conn

    # ------------------------------------------------------------------
    # proposal generation
    # ------------------------------------------------------------------
    def _collect_metadata(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT m.msg_id, m.message_type, m.timestamp,
                      d.title, d.summary, d.keywords, d.entities
               FROM documents d JOIN messages m ON d.msg_id = m.msg_id
               ORDER BY m.timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            kw, ent = [], []
            try:
                kw = json.loads(r["keywords"]) if r["keywords"] else []
            except json.JSONDecodeError:
                pass
            try:
                ent = json.loads(r["entities"]) if r["entities"] else []
            except json.JSONDecodeError:
                pass
            out.append({
                "msg_id": r["msg_id"],
                "type": r["message_type"],
                "date": r["timestamp"][:10],
                "title": r["title"],
                "summary": (r["summary"] or "")[:100],
                "keywords": kw,
                "entities": ent,
            })
        return out

    def generate_proposals(self) -> List[Dict[str, Any]]:
        """Ask the LLM for merge candidates.  Returns [] on failure/no candidates."""
        items = self._collect_metadata()
        if len(items) < 2:
            return []
        # Skip items already covered by a recent proposal (avoid re-pushing).
        user = desensitize(json.dumps(items, ensure_ascii=False))[:8000]
        try:
            resp = requests.post(
                self._cfg.llm_base_url.rstrip("/") + "/chat/completions",
                json={
                    "model": self._cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": _PROPOSAL_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": 800,
                },
                headers={"Authorization": f"Bearer {self._cfg.llm_api_key}"},
                timeout=60,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001 - daily job must not crash
            logger.warning("distill LLM call failed: %s", exc)
            return []

        t = content
        if t.startswith("```"):
            t = t.split("```", 2)[1]
            if t.startswith("json"):
                t = t[4:]
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return []
        proposals = data.get("proposals") or []
        valid = []
        for p in proposals[:5]:
            if not isinstance(p, dict):
                continue
            kind = p.get("kind")
            if kind == "alias" and p.get("canonical") and p.get("variant"):
                valid.append(p)
            elif kind == "version_of" and p.get("newer_msg_id") and p.get("older_msg_id"):
                valid.append(p)
            elif kind in ("duplicate", "link") and p.get("msg_ids"):
                valid.append(p)
        return valid

    # ------------------------------------------------------------------
    # daily run bookkeeping
    # ------------------------------------------------------------------
    def should_run_today(self) -> bool:
        """True when no proposal round has been recorded today."""
        row = self._conn.execute(
            "SELECT proposed_at FROM merge_proposals"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return True
        return row["proposed_at"][:10] != datetime.now().strftime("%Y-%m-%d")

    def record_round(self, proposals: List[Dict[str, Any]]) -> int:
        """Persist a proposal round; returns the round id (row count base)."""
        cur = self._conn.execute(
            "INSERT INTO merge_proposals (proposed_at, payload, status)"
            " VALUES (?,?,?)",
            (datetime.now().isoformat(),
             json.dumps(proposals, ensure_ascii=False), "pending"),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_pending_round(self) -> Optional[Dict[str, Any]]:
        """The most recent pending round (proposals awaiting user action)."""
        row = self._conn.execute(
            "SELECT id, proposed_at, payload FROM merge_proposals"
            " WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        try:
            proposals = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return {"round_id": row["id"], "proposed_at": row["proposed_at"],
                "proposals": proposals}

    def set_round_status(self, round_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE merge_proposals SET status = ? WHERE id = ?",
            (status, round_id),
        )
        self._conn.commit()

    def confirm_proposal(self, round_id: int, index: int) -> Optional[Dict[str, Any]]:
        """Apply proposal #index (1-based) of a pending round into
        knowledge_links.  Returns the applied proposal."""
        rnd = self.get_pending_round()
        if not rnd or rnd["round_id"] != round_id:
            return None
        proposals = rnd["proposals"]
        if index < 1 or index > len(proposals):
            return None
        p = proposals[index - 1]
        if p.get("kind") == "version_of":
            # Store the version chain: canonical = newer msg_id, variant = older.
            canonical, variant = p.get("newer_msg_id", ""), p.get("older_msg_id", "")
            msg_ids = [canonical, variant]
        else:
            canonical, variant = p.get("canonical", ""), p.get("variant", "")
            msg_ids = p.get("msg_ids") or []
        self._conn.execute(
            """INSERT INTO knowledge_links (kind, canonical, variant, msg_ids,
               reason, created_at) VALUES (?,?,?,?,?,?)""",
            (p.get("kind"), canonical, variant,
             json.dumps(msg_ids, ensure_ascii=False),
             p.get("reason", ""), datetime.now().isoformat()),
        )
        self._conn.commit()
        return p

    # ------------------------------------------------------------------
    # retrieval enrichment
    # ------------------------------------------------------------------
    def expansion_terms(self, keywords: str) -> List[str]:
        """Alias/variant terms linked to the query keywords — the searcher
        adds these to broaden matching."""
        out = []
        rows = self._conn.execute(
            "SELECT canonical, variant FROM knowledge_links WHERE kind='alias'"
        ).fetchall()
        lowered = keywords.lower()
        for r in rows:
            if r["variant"] and r["variant"].lower() in lowered:
                out.append(r["canonical"])
            elif r["canonical"] and r["canonical"].lower() in lowered:
                out.append(r["variant"])
        return [t for t in out if t]

    def version_chain(self, msg_id: str) -> Optional[Dict[str, Any]]:
        """Return the version chain a message belongs to, or None.

        Chains are transitive: if B is version_of A and C is version_of B,
        then {A,B,C} form one chain.  canonical = newer, variant = older.
        """
        links = self._conn.execute(
            "SELECT canonical, variant FROM knowledge_links WHERE kind='version_of'"
        ).fetchall()
        if not links:
            return None
        # BFS over edges to collect the component containing msg_id.
        neighbors: Dict[str, set] = {}
        for r in links:
            new, old = r["canonical"], r["variant"]
            neighbors.setdefault(new, set()).add(old)
            neighbors.setdefault(old, set()).add(new)
        if msg_id not in neighbors:
            return None
        seen, stack = set(), [msg_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(neighbors.get(cur, ()))
        if len(seen) < 2:
            return None
        # Newest = the node never appearing as `variant` (older) inside the chain.
        older_set = {r["variant"] for r in links}
        newer_candidates = [m for m in seen if m not in older_set]
        return {"members": sorted(seen), "newest": newer_candidates[0] if newer_candidates else msg_id}
