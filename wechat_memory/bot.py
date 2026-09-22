"""Main entry: wires the WeChat connection, ingestion and search together
(DESIGN.md §10, §14).

Runtime flow per inbound message:
  - if it starts with "/": route as a command (search / pick / help / ...)
  - otherwise: ingest into the knowledge base (silent, no reply needed)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, List

from .commands.router import Command, CommandRouter
from .config import Config, load_config
from .ingest.pipeline import IngestPipeline
from .models import Message
from .search import llm_fallback as llm_fb
from .search.query_parser import parse_query
from .search.searcher import SearchHit, Searcher
from .storage.db import connect
from .storage.trash import TrashManager
from .weixin.client import WeixinClient

logger = logging.getLogger(__name__)

# Plain-text URL detection (http/https), used to classify link messages.
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+")

_TYPE_ICON = {
    "text": "💬", "document": "📄", "invoice": "🧾", "image": "🖼️",
    "link": "🔗", "binary": "📦", "other": "❓",
}

# Session state: remember the last search results per chat for /<n> picks.
# {session: [msg_id, ...]}
_last_results: Dict[str, List[str]] = {}
# Trash listing per chat for /trash N restore.  {session: [entry, ...]}
_trash_listings: Dict[str, list] = {}


class MemoryAgent:
    """Top-level application object."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.conn = connect(self.cfg.db_path)
        self.router = CommandRouter(self.cfg)
        self.pipeline = IngestPipeline(self.cfg, self.conn)
        self.searcher = Searcher(self.conn)
        self.history = llm_fb.ConversationHistory()
        self.trash = TrashManager(self.cfg, self.conn)
        self.wx = WeixinClient(self.cfg)

    # -- wiring ------------------------------------------------------------
    def _wire(self) -> None:
        self.wx.on_message(self._on_inbound)

    async def _on_inbound(self, message: Message) -> None:
        """Called for every inbound message from WeChat."""
        logger.info("inbound message: type=%s text=%r attachments=%d",
                    message.message_type, (message.text or "")[:40],
                    len(message.attachments))
        try:
            if message.text.lstrip().startswith("/"):
                await self._handle_command(message)
            else:
                self._prepare_message(message)
                try:
                    self.pipeline.ingest(message)
                    logger.info("ingested %s", message.msg_id)
                    await self._send_saved_notice(message)
                except Exception as exc:  # noqa: BLE001
                    logger.error("ingest failed: %s", exc, exc_info=True)
                    await self._safe_reply(message.session, "⚠️ 保存失败，请查看日志")
        except Exception as exc:  # noqa: BLE001
            logger.error("inbound handler error: %s", exc, exc_info=True)

    def _prepare_message(self, message: Message) -> None:
        """Pre-process an inbound message before ingestion.

        Detect plain-text URLs (https://... ) and mark the message as a link
        attachment so the pipeline fetches a snapshot (DESIGN.md §7).
        """
        text = (message.text or "").strip()
        if not text or message.attachments:
            return
        urls = _URL_RE.findall(text)
        if not urls:
            return
        # Single URL message -> link attachment. Multi-URL: take the first,
        # keep the rest in text.
        from .models import Attachment
        att = Attachment(
            subtype="link",
            filename="",
            original_name="",
            url=urls[0],
        )
        message.attachments.append(att)
        if len(urls) == 1 and text.strip() == urls[0]:
            message.text = ""
        else:
            message.text = text

    async def _send_saved_notice(self, message: Message) -> None:
        """Send a short confirmation that the message was archived."""
        if message.attachments:
            subtypes = {a.subtype for a in message.attachments}
            if subtypes == {"link"}:
                note = "已保存链接"
            else:
                kinds = ", ".join(sorted(subtypes))
                note = f"已保存（{kinds}）"
        else:
            note = "已保存"
        await self._safe_reply(message.session, f"✓ {note}")

    async def _safe_reply(self, session: str, text: str) -> None:
        try:
            await self.wx.send_text(session, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("send reply failed: %s", exc)

    async def _handle_command(self, message: Message) -> None:
        cmd = self.router.route(message.text)
        if cmd.name == "search":
            await self._reply_search(message, cmd)
        elif cmd.name == "pick":
            await self._reply_pick(message, cmd)
        elif cmd.name == "delete":
            await self._reply_delete(message, cmd)
        elif cmd.name == "undo":
            await self._reply_undo(message)
        elif cmd.name == "trash":
            await self._reply_trash(message, cmd)
        elif cmd.name in ("all", "list"):
            await self._reply_all(message)
        elif cmd.name == "help":
            await self.wx.send_text(message.session, _HELP_TEXT)
        else:
            await self.wx.send_text(message.session, f"未知指令: {cmd.raw}")

    # ------------------------------------------------------------------
    async def _reply_all(self, message: Message) -> None:
        """/all (or /list): show every archived record, newest first.

        Uses the same card list as inventory queries so /N pick, /N 删除
        and /del N all work against this listing.
        """
        from .search.query_parser import ParsedQuery
        hits = self.searcher.search(ParsedQuery(action="list"), limit=50)
        if not hits:
            await self.wx.send_text(message.session, "📭 知识库还是空的，先给它发点内容吧")
            return
        _last_results[message.session] = [h.msg_id for h in hits]
        lines = [f"📭 全部记录（共 {len(hits)} 条，最近优先）：", ""]
        for i, h in enumerate(hits, 1):
            icon = _TYPE_ICON.get(h.message_type, "❓")
            title = h.title or "(无标题)"
            lines.append(f"{i}. {icon} {h.timestamp:%m-%d} | {title[:40]}")
        lines.append("")
        lines.append("回复 /1 /2 ... 查看详情并取回原文件；/N 删除 删除某条")
        await self.wx.send_text(message.session, "\n".join(lines))

    # ------------------------------------------------------------------
    async def _reply_delete(self, message: Message, cmd: Command) -> None:
        results = _last_results.get(message.session, [])
        if cmd.pick_index is None or cmd.pick_index < 1 or cmd.pick_index > len(results):
            await self.wx.send_text(
                message.session, "没有这个条目，请先 /搜索 再选择，或用 /help")
            return
        msg_id = results[cmd.pick_index - 1]
        if self.trash.soft_delete(msg_id):
            results.pop(cmd.pick_index - 1)
            await self._safe_reply(message.session,
                                   f"🗑 已删除（7 天内可用 /undo 撤销）")
        else:
            await self._safe_reply(message.session, "该记录已不存在")

    async def _reply_undo(self, message: Message) -> None:
        msg_id = self.trash.undo_last()
        if msg_id:
            await self._safe_reply(message.session, f"✓ 已恢复 {msg_id}")
        else:
            await self._safe_reply(message.session, "回收站为空，没有可恢复的记录")

    async def _reply_trash(self, message: Message, cmd: Command) -> None:
        entries = self.trash.list_entries()
        if not entries:
            await self._safe_reply(message.session, "🗑 回收站为空")
            return
        _trash_listings[message.session] = entries
        lines = [f"🗑 回收站 {len(entries)} 条（7 天后自动清除）：", ""]
        for i, e in enumerate(entries, 1):
            title = e["title"] or "(无标题)"
            lines.append(f"{i}. {e['deleted_at'][:10]} | {title[:36]}")
        lines.append("")
        lines.append("回复 /trash N 恢复第 N 条；/undo 恢复最近一条")
        await self.wx.send_text(message.session, "\n".join(lines))

        # /trash N -> restore entry N
        rest = message.text.lstrip("/").strip().split(maxsplit=1)
        if len(rest) > 1 and rest[1].strip().isdigit():
            idx = int(rest[1].strip())
            msg_id = self.trash.undo_by_index(
                idx, _trash_listings.get(message.session, []))
            if msg_id:
                await self._safe_reply(message.session, f"✓ 已恢复 {msg_id}")
            else:
                await self._safe_reply(message.session, "恢复失败，条目不存在")

    # ------------------------------------------------------------------
    async def _reply_search(self, message: Message, cmd: Command) -> None:
        raw_text = message.text.lstrip().lstrip("/").strip()

        # Layer 1: rule parse.
        query = cmd.query or parse_query(raw_text, self.cfg)

        # Inventory intent ("保存了哪些文件"): browse recent records with
        # optional type/time filters — no keyword search involved.
        if query.action == "list":
            hits = self.searcher.search(query, limit=20)
            if not hits:
                await self.wx.send_text(
                    message.session,
                    f"📭 目前还没有{('该类型' if query.type_filter else '')}的记录",
                )
                return
            _last_results[message.session] = [h.msg_id for h in hits]
            lines = [f"📭 共 {len(hits)} 条记录（最近优先）：", ""]
            for i, h in enumerate(hits, 1):
                icon = _TYPE_ICON.get(h.message_type, "❓")
                title = h.title or "(无标题)"
                lines.append(f"{i}. {icon} {h.timestamp:%m-%d} | {title[:40]}")
            lines.append("")
            lines.append("回复 /1 /2 ... 查看详情并取回原文件")
            await self.wx.send_text(message.session, "\n".join(lines))
            return

        # Layer 2: LLM parse when rules couldn't handle it (anaphora or empty).
        if (not query.keywords and not query.type_filter and not query.time_from) \
                or llm_fb.has_anaphora(raw_text):
            llm_q = llm_fb.llm_parse_query(self.cfg, raw_text, self.history)
            if llm_q is not None and (llm_q.keywords or llm_q.type_filter):
                query = llm_q

        hits = self.searcher.search(query)

        # Zero-hit fallback: LLM picks from snapshot or rewrites the query.
        if not hits and raw_text:
            snapshot = llm_fb.build_kb_snapshot(self.conn)
            rw = llm_fb.llm_rewrite_zero_hit(self.cfg, raw_text, self.history,
                                             snapshot)
            if rw is not None:
                if rw.match_id:
                    detail = self.searcher.get_detail(rw.match_id)
                    if detail is not None:
                        hits = [self._hit_from_detail(detail)]
                elif rw.rewritten is not None:
                    hits = self.searcher.search(rw.rewritten)
                    if hits:
                        query = rw.rewritten

        if not hits:
            await self.wx.send_text(
                message.session,
                f"🔍 没有找到匹配的记录（条件: {query.describe()}）",
            )
            self.history.record(raw_text, query.describe(), [])
            return

        # Record this round (for next query's anaphora resolution).
        self.history.record(raw_text, query.describe(), [h.title for h in hits])
        _last_results[message.session] = [h.msg_id for h in hits]

        lines = [f"🔍 找到 {len(hits)} 条相关记录：", ""]
        for i, h in enumerate(hits, 1):
            icon = _TYPE_ICON.get(h.message_type, "❓")
            lines.append(
                f"{i}. {icon} {h.message_type} | {h.timestamp:%Y-%m-%d} | {h.title}"
            )
            if h.summary:
                lines.append(f"   > {h.summary[:60]}")
            lines.append("")
        lines.append("回复 /1 /2 ... 查看详情并取回原文件")
        await self.wx.send_text(message.session, "\n".join(lines))

    def _hit_from_detail(self, detail: dict) -> SearchHit:
        """Build a SearchHit from get_detail() output (LLM direct pick)."""
        m = detail["message"]
        docs = detail["documents"]
        title = docs[0]["title"] if docs and docs[0]["title"] else ""
        summary = docs[0]["summary"] if docs and docs[0]["summary"] else ""
        ts = m["timestamp"]
        try:
            from datetime import datetime
            ts_dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            from datetime import datetime
            ts_dt = datetime.now()
        return SearchHit(
            msg_id=m["msg_id"], timestamp=ts_dt,
            message_type=m["message_type"], title=title, summary=summary,
            match_reason="近似匹配（LLM）", raw_dir=m["raw_dir"],
        )

    async def _reply_pick(self, message: Message, cmd: Command) -> None:
        results = _last_results.get(message.session, [])
        if cmd.pick_index is None or cmd.pick_index < 1 or cmd.pick_index > len(results):
            await self.wx.send_text(
                message.session, "没有这个条目，请先 /搜索 再选择，或用 /help")
            return
        msg_id = results[cmd.pick_index - 1]
        detail = self.searcher.get_detail(msg_id)
        if detail is None:
            await self.wx.send_text(message.session, "该记录已不存在")
            return
        m = detail["message"]
        type_icon = _TYPE_ICON.get(m["message_type"], "❓")
        lines = [f"{type_icon} {m['msg_id']}", f"时间: {m['timestamp']}",
                 f"类型: {m['message_type']}", ""]
        docs = detail["documents"]
        if docs:
            d = docs[0]
            if d["title"]:
                lines.append(f"标题: {d['title']}")
            if d["summary"]:
                lines.append(f"摘要: {d['summary']}")
            if d["category"]:
                lines.append(f"分类: {d['category']}")
        inv = detail["invoice"]
        if inv:
            lines.append(f"发票: 金额¥{inv['amount']} {inv['seller']} "
                         f"{inv['invoice_date']}")
        await self.wx.send_text(message.session, "\n".join(lines))

        # Send back originals: link attachments get their URL (from the
        # links table), file attachments get the raw file.
        links = self.conn.execute(
            "SELECT url FROM links WHERE msg_id = ?", (msg_id,)
        ).fetchall()
        for l in links:
            if l["url"]:
                await self._safe_reply(message.session, l["url"])
        for att in detail["attachments"]:
            if not att["filename"]:
                continue
            path = f"{m['raw_dir']}/{att['filename']}"
            try:
                await self.wx.send_file(message.session, path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("send file failed: %s", exc)

    # ------------------------------------------------------------------
    async def run(self) -> None:
        self._wire()
        # Purge trash entries older than the retention window.
        purged = self.trash.purge_expired()
        if purged:
            logger.info("purged %d expired trash entries at startup", purged)
        if not self.wx.has_credentials():
            logger.info("no Weixin credentials; starting QR login...")
            creds = await self.wx.qr_login()
            if not creds:
                logger.error("QR login failed; exiting")
                return
        await self.wx.connect()
        logger.info("wechat-memory-agent started")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.wx.disconnect()


_HELP_TEXT = """\
可用指令：
  / <你想找的内容>  自然语言检索，如：/帮我查最近一个月的压缩包
  /all 或 /list     显示全部记录列表
  /1 /2 ...         选择上一条搜索结果中的某条，回传原文件
  /1 删除  或 /del 1   删除上次结果中的第 1 条（7 天内可撤销）
  /undo             撤销最近一次删除
  /trash            查看回收站；/trash N 恢复第 N 条
  /help             显示本帮助
"""


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    agent = MemoryAgent()
    asyncio.run(agent.run())
