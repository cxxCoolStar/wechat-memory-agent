"""WeChat iLink Bot API client (ported from hermes gateway/platforms/weixin.py).

This module is the only place that talks to Tencent's iLink API:
QR login, long-poll getupdates, message parsing, AES media download,
and outbound send.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

from ..config import Config
from ..models import Attachment, Message
from . import crypto

logger = logging.getLogger(__name__)

# --- iLink protocol constants (from hermes weixin.py) -----------------------
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2

# item_list types
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# message types
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

_WEIXIN_CDN_ALLOWLIST = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


# --- helpers ---------------------------------------------------------------

def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _account_dir(data_home: Path) -> Path:
    path = data_home / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(data_home: Path, account_id: str) -> Path:
    return _account_dir(data_home) / f"{account_id}.json"


def _sync_buf_path(data_home: Path, account_id: str) -> Path:
    return _account_dir(data_home) / f"{account_id}.sync.json"


def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    from urllib.parse import quote
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _assert_weixin_cdn_url(url: str) -> None:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise RuntimeError(f"refusing non-Weixin CDN url: {url}")


def _media_reference(item: Dict[str, Any], key: str) -> Dict[str, Any]:
    return (item.get(key) or {}).get("media") or {}


def _mime_from_filename(filename: str) -> str:
    import mimetypes
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    """Extract text from item_list, mirroring hermes logic (refs included)."""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: List[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_item = item.get("voice_item") or {}
            if not (voice_item.get("media") or {}):
                voice_text = str(voice_item.get("text") or "")
                if voice_text:
                    return f"[Voice transcription provided by Weixin]\n{voice_text}"
            continue
    return ""


def _guess_chat_type(message: Dict[str, Any], account_id: str) -> Tuple[str, str]:
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id
        and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


class WeixinClient:
    """Owns the aiohttp session, poll loop and inbound dispatch."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._sync_buf = ""
        self._on_message: Optional[Callable[[Message], None]] = None

        self._account_id = cfg.wx_account_id
        self._token = cfg.wx_token
        self._base_url = cfg.wx_base_url
        self._cdn_base_url = cfg.wx_cdn_base_url

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    def has_credentials(self) -> bool:
        return bool(self._account_id and self._token)

    def _save_account(self, account_id: str, token: str, base_url: str,
                      user_id: str) -> None:
        path = _account_file(self._cfg.data_home, account_id)
        path.write_text(
            json.dumps({"token": token, "base_url": base_url,
                        "user_id": user_id, "saved_at": time.time()}),
            encoding="utf-8",
        )

    def _load_account(self) -> Optional[Dict[str, Any]]:
        if not self._account_id:
            return None
        path = _account_file(self._cfg.data_home, self._account_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _persist_credentials_to_env(self, account_id: str, token: str,
                                    base_url: str) -> None:
        """Write credentials into the project .env so next start auto-connects."""
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        lines = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        updates = {
            "WEIXIN_ACCOUNT_ID": account_id,
            "WEIXIN_TOKEN": token,
            "WEIXIN_BASE_URL": base_url,
        }
        seen = set()
        out = []
        for line in lines:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                out.append(line)
        for key, val in updates.items():
            if key not in seen:
                out.append(f"{key}={val}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _api_post(self, endpoint: str, payload: Dict[str, Any],
                        token: Optional[str], timeout_ms: int,
                        base_url: Optional[str] = None) -> Dict[str, Any]:
        assert self._session is not None
        body = _json_dumps({**payload, "base_info": _base_info()})
        url = f"{(base_url or self._base_url).rstrip('/')}/{endpoint}"

        async def _do() -> Dict[str, Any]:
            async with self._session.post(url, data=body,
                                          headers=_headers(token, body)) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(
                        f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def _api_get(self, endpoint: str, timeout_ms: int,
                       base_url: Optional[str] = None) -> Dict[str, Any]:
        assert self._session is not None
        url = f"{(base_url or self._base_url).rstrip('/')}/{endpoint}"
        headers = {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }

        async def _do() -> Dict[str, Any]:
            async with self._session.get(url, headers=headers) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(
                        f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def _download_bytes(self, url: str,
                              timeout_seconds: float = 60.0) -> bytes:
        assert self._session is not None

        async def _do() -> bytes:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()

        return await asyncio.wait_for(_do(), timeout=timeout_seconds)

    # ------------------------------------------------------------------
    # QR login
    # ------------------------------------------------------------------
    async def qr_login(self, bot_type: str = "3",
                       timeout_seconds: int = 480) -> Optional[Dict[str, str]]:
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for Weixin QR login")

        async with aiohttp.ClientSession(trust_env=True) as session:
            self._session = session
            try:
                qr_resp = await self._api_get(
                    f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                    timeout_ms=QR_TIMEOUT_MS,
                    base_url=ILINK_BASE_URL,
                )
            except Exception as exc:
                logger.error("failed to fetch QR code: %s", exc)
                return None

            qrcode_value = str(qr_resp.get("qrcode") or "")
            qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
            if not qrcode_value:
                logger.error("QR response missing qrcode")
                return None

            qr_scan_data = qrcode_url if qrcode_url else qrcode_value
            print("\n请使用微信扫描以下二维码：")
            if qrcode_url:
                print(qrcode_url)
            try:
                import qrcode
                qr = qrcode.QRCode()
                qr.add_data(qr_scan_data)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except Exception as qr_exc:
                print(f"（终端二维码渲染失败: {qr_exc}，请直接打开上面的链接）")

            deadline = time.monotonic() + timeout_seconds
            current_base_url = ILINK_BASE_URL
            refresh_count = 0

            while time.monotonic() < deadline:
                try:
                    status_resp = await self._api_get(
                        f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                        timeout_ms=QR_TIMEOUT_MS,
                        base_url=current_base_url,
                    )
                except asyncio.TimeoutError:
                    await asyncio.sleep(1)
                    continue
                except Exception:
                    await asyncio.sleep(1)
                    continue

                status = str(status_resp.get("status") or "wait")
                if status == "wait":
                    print(".", end="", flush=True)
                elif status == "scaned":
                    print("\n已扫码，请在微信里确认...")
                elif status == "scaned_but_redirect":
                    redirect_host = str(status_resp.get("redirect_host") or "")
                    if redirect_host:
                        current_base_url = f"https://{redirect_host}"
                elif status == "expired":
                    refresh_count += 1
                    if refresh_count > 3:
                        print("\n二维码多次过期，请重新执行登录。")
                        return None
                    print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                    try:
                        qr_resp = await self._api_get(
                            f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                            timeout_ms=QR_TIMEOUT_MS,
                            base_url=ILINK_BASE_URL,
                        )
                        qrcode_value = str(qr_resp.get("qrcode") or "")
                        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                        if qrcode_url:
                            print(qrcode_url)
                    except Exception:
                        return None
                elif status == "confirmed":
                    account_id = str(status_resp.get("ilink_bot_id") or "")
                    token = str(status_resp.get("bot_token") or "")
                    base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                    user_id = str(status_resp.get("ilink_user_id") or "")
                    if not account_id or not token:
                        logger.error("QR confirmed but credentials incomplete")
                        return None
                    # Sync credentials onto this instance so connect()
                    # succeeds right after login without a restart.
                    self._account_id = account_id
                    self._token = token
                    self._base_url = base_url
                    self._save_account(account_id, token, base_url, user_id)
                    self._persist_credentials_to_env(account_id, token, base_url)
                    print(f"\n微信连接成功，account_id={account_id}")
                    return {
                        "account_id": account_id, "token": token,
                        "base_url": base_url, "user_id": user_id,
                    }
                await asyncio.sleep(1)

            print("\n微信登录超时。")
            return None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        if aiohttp is None:
            raise RuntimeError("aiohttp is required")
        if not self.has_credentials():
            raise RuntimeError("no Weixin credentials; run qr_login() first")

        # Restore token/base_url from disk if env only had account_id.
        if not self._token:
            saved = self._load_account()
            if saved:
                self._token = saved.get("token") or self._token
                self._base_url = saved.get("base_url") or self._base_url

        self._session = aiohttp.ClientSession(trust_env=True)
        self._sync_buf = self._load_sync_buf()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="wma-poll")
        logger.info("weixin connected: account=%s", self._account_id)

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    def _load_sync_buf(self) -> str:
        path = _sync_buf_path(self._cfg.data_home, self._account_id)
        if not path.exists():
            return ""
        try:
            return json.loads(path.read_text(encoding="utf-8")).get(
                "get_updates_buf", "")
        except Exception:
            return ""

    def _save_sync_buf(self, sync_buf: str) -> None:
        path = _sync_buf_path(self._cfg.data_home, self._account_id)
        path.write_text(json.dumps({"get_updates_buf": sync_buf}),
                        encoding="utf-8")

    async def _get_updates(self) -> Dict[str, Any]:
        try:
            return await self._api_post(
                EP_GET_UPDATES,
                payload={"get_updates_buf": self._sync_buf},
                token=self._token,
                timeout_ms=LONG_POLL_TIMEOUT_MS,
            )
        except asyncio.TimeoutError:
            return {"ret": 0, "msgs": [], "get_updates_buf": self._sync_buf}

    async def _poll_loop(self) -> None:
        consecutive_failures = 0
        while self._running:
            try:
                resp = await self._get_updates()
                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)

                if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                    logger.warning("weixin session expired; sleeping 600s")
                    consecutive_failures = 0
                    await asyncio.sleep(600)
                    continue

                if ret != 0 or errcode != 0:
                    consecutive_failures += 1
                    delay = (RETRY_DELAY_SECONDS if consecutive_failures
                             < MAX_CONSECUTIVE_FAILURES else BACKOFF_DELAY_SECONDS)
                    logger.warning("getupdates error ret=%s err=%s; retry in %ss",
                                   ret, errcode, delay)
                    await asyncio.sleep(delay)
                    continue

                consecutive_failures = 0
                new_buf = str(resp.get("get_updates_buf") or "")
                if new_buf:
                    self._sync_buf = new_buf
                    self._save_sync_buf(new_buf)
                for msg in resp.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(msg))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    async def _process_message_safe(self, message: Dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except Exception as exc:
            logger.error("inbound error: %s", exc, exc_info=True)

    async def _process_message(self, message: Dict[str, Any]) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return
        if not self._on_message:
            return

        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        chat_type, chat_id = _guess_chat_type(message, self._account_id)
        if chat_type == "group":
            # iLink bots generally don't receive group events; skip if any.
            return

        parsed = await self.parse_message(message)
        await self._dispatch_inbound(parsed)

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------
    async def _download_and_decrypt_media(
        self, encrypted_query_param: Optional[str], aes_key_b64: Optional[str],
        full_url: Optional[str], timeout_seconds: float,
    ) -> bytes:
        if encrypted_query_param:
            raw = await self._download_bytes(
                _cdn_download_url(self._cdn_base_url, encrypted_query_param),
                timeout_seconds=timeout_seconds,
            )
        elif full_url:
            _assert_weixin_cdn_url(full_url)
            raw = await self._download_bytes(full_url,
                                             timeout_seconds=timeout_seconds)
        else:
            raise RuntimeError("media item had neither encrypt_query_param nor full_url")
        if aes_key_b64:
            raw = crypto.aes128_ecb_decrypt(raw, crypto.parse_aes_key(aes_key_b64))
        return raw

    async def _download_image(self, item: Dict[str, Any]) -> Optional[bytes]:
        media = _media_reference(item, "image_item")
        aeskey = (item.get("image_item") or {}).get("aeskey")
        if aeskey:
            aes_key_b64 = base64.b64encode(
                bytes.fromhex(str(aeskey))).decode("ascii")
        else:
            aes_key_b64 = media.get("aes_key")
        try:
            return await self._download_and_decrypt_media(
                media.get("encrypt_query_param"), aes_key_b64,
                media.get("full_url"), 30.0,
            )
        except Exception as exc:
            logger.warning("image download failed: %s", exc)
            return None

    async def _download_file(self, item: Dict[str, Any]) -> Optional[bytes]:
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        try:
            return await self._download_and_decrypt_media(
                media.get("encrypt_query_param"), media.get("aes_key"),
                media.get("full_url"), 60.0,
            )
        except Exception as exc:
            logger.warning("file download failed: %s", exc)
            return None

    async def _download_voice(self, item: Dict[str, Any]) -> Optional[bytes]:
        media = _media_reference(item, "voice_item")
        try:
            return await self._download_and_decrypt_media(
                media.get("encrypt_query_param"), media.get("aes_key"),
                media.get("full_url"), 30.0,
            )
        except Exception as exc:
            logger.warning("voice download failed: %s", exc)
            return None

    async def _download_video(self, item: Dict[str, Any]) -> Optional[bytes]:
        media = _media_reference(item, "video_item")
        try:
            return await self._download_and_decrypt_media(
                media.get("encrypt_query_param"), media.get("aes_key"),
                media.get("full_url"), 120.0,
            )
        except Exception as exc:
            logger.warning("video download failed: %s", exc)
            return None

    async def parse_message(self, message: Dict[str, Any]) -> Message:
        """Convert a raw iLink message dict into our Message model.

        Downloads media bytes into the message's raw dir.
        """
        from ..models import gen_msg_id

        sender_id = str(message.get("from_user_id") or "")
        text = _extract_text(message.get("item_list") or [])
        item_list = message.get("item_list") or []
        ts = _now()

        msg = Message(
            msg_id="",
            timestamp=ts,
            session=sender_id,
            sender_id=sender_id,
            message_type="text",
            raw_dir="",
            text=text,
            raw_message=message,
        )
        msg.msg_id = gen_msg_id(message, ts)

        # Raw dir: data_home/raw/<msg_id>
        raw_dir = self._cfg.raw_dir / msg.msg_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        msg.raw_dir = str(raw_dir)

        for item in item_list:
            itype = item.get("type")
            if itype == ITEM_TEXT:
                continue  # text already captured
            data = None
            fname = ""
            ext = ".bin"
            if itype == ITEM_IMAGE:
                data = await self._download_image(item)
                fname = f"image_{len(msg.attachments) + 1}.jpg"
                ext = ".jpg"
            elif itype == ITEM_FILE:
                data = await self._download_file(item)
                fname = str((item.get("file_item") or {}).get("file_name")
                            or f"file_{len(msg.attachments) + 1}.bin")
                ext = Path(fname).suffix or ".bin"
            elif itype == ITEM_VOICE:
                data = await self._download_voice(item)
                fname = f"voice_{len(msg.attachments) + 1}.silk"
                ext = ".silk"
            elif itype == ITEM_VIDEO:
                data = await self._download_video(item)
                fname = f"video_{len(msg.attachments) + 1}.mp4"
                ext = ".mp4"

            if data is None:
                logger.warning("media download failed for item type %s", itype)
                continue

            # Write bytes into the raw dir (original file, untouched).
            safe_name = Path(fname).name
            out_path = raw_dir / safe_name
            out_path.write_bytes(data)

            att = Attachment(
                subtype="unknown",
                filename=safe_name,
                original_name=safe_name,
                size_bytes=len(data),
                file_hash="",  # filled by pipeline if needed
            )
            msg.attachments.append(att)

        return msg

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------
    async def send_text(self, chat_id: str, text: str) -> None:
        message = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": f"wma-{uuid.uuid4().hex}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        await self._api_post(EP_SEND_MESSAGE, {"msg": message},
                             token=self._token, timeout_ms=API_TIMEOUT_MS)

    # ------------------------------------------------------------------
    # callback
    # ------------------------------------------------------------------
    def on_message(self, handler: Callable[[Message], Any]) -> None:
        self._on_message = handler

    async def _dispatch_inbound(self, message: Message) -> None:
        """Call the inbound handler, awaiting it if it's a coroutine."""
        if self._on_message is None:
            return
        result = self._on_message(message)
        if asyncio.iscoroutine(result):
            await result


def _now():
    from datetime import datetime
    return datetime.now()
