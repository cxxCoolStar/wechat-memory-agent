"""WeChat iLink Bot API client (port from hermes gateway/platforms/weixin.py).

This module is the only place that talks to Tencent's iLink API:
QR login, long-poll getupdates, message parsing, AES media download,
and outbound send.  All protocol constants and crypto helpers will live
here or in crypto.py.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ..config import Config
from ..models import Message

# iLink protocol constants (from hermes weixin.py)
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
LONG_POLL_TIMEOUT_MS = 35_000


class WeixinClient:
    """Owns the aiohttp session, poll loop and inbound dispatch."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._session: Any = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        """Restore credentials and start the long-poll loop."""
        # TODO(impl): load saved account json, create aiohttp session,
        #   create_task(self._poll_loop())
        raise NotImplementedError

    async def disconnect(self) -> None:
        # TODO(impl)
        raise NotImplementedError

    # -- login -------------------------------------------------------------
    async def qr_login(self) -> Dict[str, str]:
        """QR login flow; returns {account_id, token, base_url, user_id}."""
        # TODO(impl): GET qr -> poll status -> confirmed -> save account
        raise NotImplementedError

    # -- polling -----------------------------------------------------------
    async def _poll_loop(self) -> None:
        # TODO(impl): getupdates long-poll, sync_buf persistence,
        #   retry/backoff, dispatch each message to on_message
        raise NotImplementedError

    # -- inbound parsing ---------------------------------------------------
    def parse_message(self, raw: Dict[str, Any]) -> Message:
        """Convert a raw iLink message dict into our Message model."""
        # TODO(impl): _extract_text, _guess_chat_type, media download+decrypt
        raise NotImplementedError

    # -- outbound ----------------------------------------------------------
    async def send_text(self, chat_id: str, text: str) -> None:
        """Send a text reply (used for search results)."""
        # TODO(impl): sendmessage + context_token
        raise NotImplementedError

    async def send_file(self, chat_id: str, local_path: str) -> None:
        """Send a file back to the user (used by /<n> to return originals)."""
        # TODO(impl): getuploadurl + AES encrypt + upload + sendmessage
        raise NotImplementedError

    # -- callback ----------------------------------------------------------
    def on_message(self, handler) -> None:
        """Set the inbound handler (called for every non-command message)."""
        self._on_message = handler  # type: ignore[attr-defined]
