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
from .search.searcher import SearchHit, Searcher
from .storage.db import connect
from .weixin.client import WeixinClient

logger = logging.getLogger(__name__)

# Plain-text URL detection (http/https), used to classify link messages.
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+")

_TYPE_ICON = {
    "text": "💬", "document": "📄", "invoice": "🧾", "image": "🖼️",
    "link": "🔗", "binary": "📦", "other": "❓",
}

# Session state: remember the last search results per chat for /<n> picks.
# {session: [(msg_id, label), ...]}
_last_results: Dict[str, List[str]] = {}


class MemoryAgent:
    """Top-level application object."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.conn = connect(self.cfg.db_path)
        self.router = CommandRouter(self.cfg)
        self.pipeline = IngestPipeline(self.cfg, self.conn)
        self.searcher = Searcher(self.conn)
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
        elif cmd.name == "help":
            await self.wx.send_text(message.session, _HELP_TEXT)
        else:
            await self.wx.send_text(message.session, f"未知指令: {cmd.raw}")

    # ------------------------------------------------------------------
    async def _reply_search(self, message: Message, cmd: Command) -> None:
        if cmd.query is None:
            await self.wx.send_text(message.session, "没理解你的查询，试试 /help")
            return
        hits = self.searcher.search(cmd.query)
        if not hits:
            await self.wx.send_text(
                message.session,
                f"🔍 没有找到匹配的记录（条件: {cmd.query.describe()}）",
            )
            return
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
        lines = [f"📄 {m['msg_id']}", f"时间: {m['timestamp']}",
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

        # Send back original files, if any.
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
  /1 /2 ...         选择上一条搜索结果中的某条，回传原文件
  /help             显示本帮助
"""


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    agent = MemoryAgent()
    asyncio.run(agent.run())
