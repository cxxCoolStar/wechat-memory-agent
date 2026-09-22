"""Offline end-to-end simulation with real sample files.

Shows the whole pipeline WITHOUT WeChat:  type detect -> extract -> LLM
metadata -> store -> index -> search -> render cards.

Usage (from project root):
    .venv/Scripts/python scripts/simulate.py
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wechat_memory.config import load_config
from wechat_memory.ingest.pipeline import IngestPipeline
from wechat_memory.models import Attachment, Message
from wechat_memory.search.query_parser import parse_query
from wechat_memory.search.searcher import Searcher
from wechat_memory.storage.db import connect

SAMPLE_ROOT = Path(
    r"C:/Users/asta1/xwechat_files/wxid_gc9ooxdzcgnb22_a7a0/temp/RWTemp"
)

_SAMPLES = [
    # (label, session, days_ago, file path or None, text)
    (
        "text_ad", "self", 1, None,
        "最近刚面完深圳一家做电商业务的 AI 应用开发岗位（标薪 25K-40K），"
        "顺利拿到一面通过！开源贡献 PR 的经历非常加分……",
    ),
    (
        "invoice", "self", 30, SAMPLE_ROOT
        / "2026-08/9e20f478899dc29eb19741386f9343c8"
        / "dzfp_26442000009628529551_广州市白云区嘉禾名扬煮艺杨记火锅店_20260822154402(1).pdf",
        "",
    ),
    (
        "screenshot", "self", 20, SAMPLE_ROOT
        / "2026-09/9e20f478899dc29eb19741386f9343c8"
        / "89ff6a054cda4f038a03f9fa7fee3341.jpg",
        "",
    ),
    (
        "source_zip", "self", 10, SAMPLE_ROOT
        / "2026-09/9e20f478899dc29eb19741386f9343c8"
        / "wx-cli-again-main.zip",
        "",
    ),
    (
        "big_jar", "self", 3, SAMPLE_ROOT
        / "2026-09/9e20f478899dc29eb19741386f9343c8"
        / "iam-console(3).jar",
        "",
    ),
    (
        "link", "self", 2, None,
        "https://github.com/Sherlockwz/T-Mem",
    ),
]

# 链接样本单独标记：text 字段即 URL，构造为 LINK 附件
_LINK_LABELS = {"link"}

_TYPE_ICON = {
    "text": "💬", "document": "📄", "invoice": "🧾", "image": "🖼️",
    "link": "🔗", "binary": "📦", "other": "❓",
}


def build_message(label: str, session: str, days_ago: int, path: Path | None,
                  text: str, ts: datetime) -> Message:
    raw_message = {"from_user_id": "me", "message_id": label}
    msg = Message(
        msg_id=f"{ts:%Y%m%d%H%M%S}-{label}",
        timestamp=ts,
        session=session,
        sender_id="me",
        message_type="text",
        raw_dir="",
        text=text,
        raw_message=raw_message,
    )
    if path and path.exists():
        dst = Path(path.name)
        att = Attachment(
            subtype="unknown",
            filename=dst.name,
            original_name=path.name,
            size_bytes=path.stat().st_size,
        )
        msg.attachments.append(att)
        # raw dir is created by pipeline; copy the file there.
        msg.raw_dir = ""  # filled below
    return msg


def main() -> None:
    cfg = load_config()
    # Use a fresh data dir for the simulation.
    sim_home = cfg.data_home.parent / "wma_sim"
    if sim_home.exists():
        shutil.rmtree(sim_home)
    sim_home.mkdir(parents=True, exist_ok=True)
    cfg.data_home = sim_home
    cfg.raw_dir = sim_home / "raw"
    cfg.extracted_dir = sim_home / "extracted"
    cfg.db_path = sim_home / "index.db"
    cfg.ensure_dirs()

    conn = connect(cfg.db_path)
    pipeline = IngestPipeline(cfg, conn)
    now = datetime.now()

    print("=" * 60)
    print("离线模拟：真实样本 → 入库 → 检索")
    print("数据目录:", sim_home)
    print("=" * 60)

    # 1. Ingest all samples.
    for label, session, days_ago, path, text in _SAMPLES:
        ts = now - timedelta(days=days_ago)
        msg = build_message(label, session, days_ago, path, text, ts)
        # Links: text field holds the URL -> construct a LINK attachment.
        if label in _LINK_LABELS:
            msg.text = ""
            att = Attachment(
                subtype="link", filename="", original_name="",
                url=text,
            )
            msg.attachments = [att]
            msg.raw_dir = str(cfg.raw_dir / msg.msg_id)
            Path(msg.raw_dir).mkdir(parents=True, exist_ok=True)
        elif msg.attachments:
            msg.raw_dir = str(cfg.raw_dir / msg.msg_id)
            # copy source file into raw dir
            src = next(p for p in _SAMPLES if p[0] == label)
            raw_dir = Path(msg.raw_dir)
            raw_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src[3], raw_dir / msg.attachments[0].filename)
        else:
            msg.raw_dir = str(cfg.raw_dir / msg.msg_id)
            Path(msg.raw_dir).mkdir(parents=True, exist_ok=True)
        print(f"\n▶ 入库: {label} ({msg.msg_id})")
        pipeline.ingest(msg)
        print(f"  类型: {msg.message_type}, 附件: {len(msg.attachments)}")

    # 2. Query.
    print("\n" + "=" * 60)
    print("检索测试")
    print("=" * 60)
    searcher = Searcher(conn)
    queries = [
        "/帮我查一下最近一个月我上传的一个压缩包",
        "/发票",
        "/关于RAG的项目",
        "/腾讯的开源项目",
        "/招聘",
        "/最近一个月的内容",
    ]
    for qtext in queries:
        q = parse_query(qtext, cfg)
        hits = searcher.search(q)
        print(f"\n🔍 {qtext}")
        print(f"   解析: {q.describe()}")
        if not hits:
            print("   (无结果)")
            continue
        for i, h in enumerate(hits, 1):
            icon = _TYPE_ICON.get(h.message_type, "❓")
            print(f"  {i}. {icon} {h.message_type} | {h.timestamp:%Y-%m-%d} | {h.title}")
            print(f"     {h.match_reason}")
            if h.summary:
                print(f"     > {h.summary[:60]}")


if __name__ == "__main__":
    main()
