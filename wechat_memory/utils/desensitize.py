"""Sensitive-information desensitization (DESIGN.md §11.2).

Applied ONLY to content being sent to the LLM for metadata generation.
The raw text stored on disk is never modified.
"""

from __future__ import annotations

import re

# Phone numbers (Chinese mobile): 1[3-9]\d{9}
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 18-digit ID card number
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# Bank card (16-19 digits)
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

_RULES = [
    ("身份证号", _ID_CARD),
    ("银行卡号", _BANK_CARD),
    ("手机号", _PHONE),
]


def _mask(match: re.Match) -> str:
    s = match.group()
    if len(s) <= 4:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


def desensitize(text: str) -> str:
    """Replace sensitive patterns with masked versions. Returns the text as-is
    when nothing matches."""
    if not text:
        return text
    out = text
    for _name, pattern in _RULES:
        out = pattern.sub(_mask, out)
    return out
