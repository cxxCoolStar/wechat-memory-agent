"""LLM-generated metadata (DESIGN.md §9.3).

Calls an OpenAI-compatible API to turn extracted text into structured
metadata (summary/keywords/category/entities/title).  Input is
desensitized before the request; output is parsed into ItemMetadata.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from ..config import Config
from ..models import InvoiceData, ItemMetadata
from ..utils.desensitize import desensitize

logger = logging.getLogger(__name__)

_METADATA_SYSTEM = """\
你是一个文档元数据提取器。根据用户提供的内容，输出 JSON，只输出 JSON，不要任何其他文字。
JSON 结构（全部字段都要）：
{
  "summary": "一句话中文摘要（≤80字）",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "category": "内容分类（如 技术文档/发票/报告/合同/截图/招聘/其他）",
  "entities": ["提到的关键实体（人名/公司/组织）"],
  "title": "简短标题（≤30字）"
}
"""


def _call_llm(cfg: Config, system: str, user: str, *, max_tokens: int = 800) -> str:
    """One chat-completions call to the configured LLM, returning the text."""
    if not cfg.llm_base_url or not cfg.llm_api_key:
        raise RuntimeError("LLM not configured: WMA_LLM_BASE_URL / WMA_LLM_API_KEY")
    url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LLM response: {data}") from exc


def _parse_json_response(text: str) -> dict:
    """Robustly parse a JSON object from an LLM reply.

    Handles: markdown code fences, leading/trailing noise, invalid JSON.
    """
    t = text.strip()
    # Strip markdown code fences if present.
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
    # Find the first {...} block as a last resort.
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start : end + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON: %r", text[:200])
        return {}
    return data if isinstance(data, dict) else {}


class LLMMetadataGenerator:
    """Generates ItemMetadata for extracted text via the LLM."""

    def __init__(self, cfg: Config):
        self._cfg = cfg

    def generate(self, text: str, *, kind: str = "document") -> ItemMetadata:
        """Produce ItemMetadata for extracted text.

        kind: "document" | "image" | "link" | "invoice"
        """
        text = (text or "").strip()
        if not text:
            return ItemMetadata()
        safe = desensitize(text[:4000])  # cap length; desensitize before send
        user = f"内容类型: {kind}\n\n内容:\n{safe}\n\n请提取元数据。"
        raw = _call_llm(self._cfg, _METADATA_SYSTEM, user)
        data = _parse_json_response(raw)
        return ItemMetadata(
            summary=str(data.get("summary", "")),
            keywords=[str(k) for k in data.get("keywords", []) if k],
            category=str(data.get("category", "")),
            entities=[str(e) for e in data.get("entities", []) if e],
            title=str(data.get("title", "")),
        )

    def generate_invoice(self, text: str) -> InvoiceData:
        """Extract structured invoice fields."""
        text = (text or "").strip()
        if not text:
            return InvoiceData()
        safe = desensitize(text[:4000])
        system = """\
你是发票信息提取器。根据发票内容输出 JSON，只输出 JSON：
{"invoice_no": "发票号码", "invoice_date": "开票日期(YYYY-MM-DD)", "amount": 金额(数字), "seller": "销售方", "buyer": "购买方", "tax_no": "纳税人识别号"}
"""
        raw = _call_llm(self._cfg, system, f"发票内容:\n{safe}")
        data = _parse_json_response(raw)
        try:
            amount = float(data.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        return InvoiceData(
            invoice_no=str(data.get("invoice_no", "")),
            invoice_date=str(data.get("invoice_date", "")),
            amount=amount,
            seller=str(data.get("seller", "")),
            buyer=str(data.get("buyer", "")),
            tax_no=str(data.get("tax_no", "")),
        )
