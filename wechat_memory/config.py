"""Configuration for wechat-memory-agent.

All paths and API settings are resolved from environment variables
(optionally via a .env file), with sensible defaults.  This module is
imported early by every other component, so it must not depend on
anything but the standard library and python-dotenv.
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_data_home() -> Path:
    """Data lives outside the code dir so backups/migration are trivial."""
    env = os.getenv("WMA_DATA_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "wechat-memory-agent-data"


def _resolve_raw_dir(data_home: Path) -> Path:
    return data_home / "raw"


def _resolve_extracted_dir(data_home: Path) -> Path:
    return data_home / "extracted"


def _resolve_db_path(data_home: Path) -> Path:
    return data_home / "index.db"


# ---------------------------------------------------------------------------
# Config object
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Runtime configuration, loaded once at startup."""

    # Paths
    data_home: Path = field(default_factory=lambda: _resolve_data_home())
    raw_dir: Path = field(default_factory=lambda: _resolve_raw_dir(_resolve_data_home()))
    extracted_dir: Path = field(default_factory=lambda: _resolve_extracted_dir(_resolve_data_home()))
    db_path: Path = field(default_factory=lambda: _resolve_db_path(_resolve_data_home()))

    # LLM API (OpenAI-compatible)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_vision_model: str = ""  # may differ from the text model

    # WeChat iLink credentials (populated after QR login)
    wx_account_id: str = ""
    wx_token: str = ""
    wx_base_url: str = "https://ilinkai.weixin.qq.com"
    wx_cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"

    # Storage tuning
    binary_size_threshold_mb: int = 10

    # Type-word map for query parsing (user voice -> type)
    type_word_map: dict = field(default_factory=dict)

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.extracted_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def _load_type_word_map() -> dict:
    """Default user-voice -> type mapping.  Users can extend via env/JSON later."""
    return {
        "压缩包": "binary", "zip": "binary", "rar": "binary",
        "发票": "invoice",
        # Note: avoid single-char "图" here — it would misfire on queries
        # like "图记忆/图书馆/地图". Use explicit words only.
        "截图": "image", "图片": "image", "照片": "image",
        "文档": "document", "pdf": "document", "word": "document", "excel": "document",
        "链接": "link", "网址": "link", "url": "link",
    }


def load_config() -> Config:
    """Build a Config from environment (and optional .env file)."""
    if load_dotenv is not None:
        load_dotenv()

    data_home = _resolve_data_home()

    cfg = Config(
        data_home=data_home,
        raw_dir=_resolve_raw_dir(data_home),
        extracted_dir=_resolve_extracted_dir(data_home),
        db_path=_resolve_db_path(data_home),
        llm_base_url=os.getenv("WMA_LLM_BASE_URL", "").strip(),
        llm_api_key=os.getenv("WMA_LLM_API_KEY", "").strip(),
        llm_model=os.getenv("WMA_LLM_MODEL", "").strip(),
        llm_vision_model=os.getenv("WMA_LLM_VISION_MODEL", "").strip(),
        wx_account_id=os.getenv("WEIXIN_ACCOUNT_ID", "").strip(),
        wx_token=os.getenv("WEIXIN_TOKEN", "").strip(),
        wx_base_url=os.getenv("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com").strip(),
        wx_cdn_base_url=os.getenv("WEIXIN_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c").strip(),
        binary_size_threshold_mb=int(os.getenv("WMA_BINARY_THRESHOLD_MB", "10")),
        type_word_map=_load_type_word_map(),
    )
    cfg.ensure_dirs()
    return cfg
