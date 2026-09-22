"""Desensitization for text sent to the LLM (DESIGN.md §11.2).

Two pattern families, merged from two sources:
- Credential/token redaction: ported from hermes ``agent/redact.py``
  (vendor API-key prefixes, KEY=value env assignments, sensitive URL query
  params, JWTs, private keys).
- Chinese PII (not in hermes): ID cards, mainland mobile numbers, bank cards.

Applied ONLY to content being sent to the LLM.  The raw text stored in
raw/ is never modified.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 1. Credential prefixes (ported from hermes redact.py)
# ---------------------------------------------------------------------------
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic
    r"ark-[A-Za-z0-9-]{10,}",           # Volcengine Ark
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xapp-\d+-[A-Za-z0-9-]{10,}",      # Slack app-level token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack bot/app/user tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key
    r"sk_test_[A-Za-z0-9]{10,}",
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace
    r"r8_[A-Za-z0-9]{10,}",             # Replicate
    r"npm_[A-Za-z0-9]{10,}",            # npm
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean
    r"gsk_[A-Za-z0-9]{10,}",            # Groq
    r"xai-[A-Za-z0-9]{30,}",            # xAI
    r"ntn_[A-Za-z0-9]{10,}",            # Notion
    r"glpat-[A-Za-z0-9_\-]{10,}",       # GitLab PAT
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily
    r"exa_[A-Za-z0-9]{10,}",            # Exa
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
]
_PREFIX_RES = [re.compile(p) for p in _PREFIX_PATTERNS]
_PREFIX_SUBSTRINGS = tuple({p[:3] for p in _PREFIX_PATTERNS if len(p) >= 3})

# ---------------------------------------------------------------------------
# 2. KEY=value env assignments (ported from hermes; simplified)
# ---------------------------------------------------------------------------
_SECRET_ENV_NAMES = r"(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PW|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Za-z0-9_]{{0,40}}{_SECRET_ENV_NAMES}[A-Za-z0-9_]{{0,40}})\s*[=:]\s*(['\"]?)(\S{{4,}})\2",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 3. Sensitive URL query params (ported from hermes)
# ---------------------------------------------------------------------------
_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "api_key",
    "apikey", "client_secret", "password", "auth", "jwt", "session",
    "secret", "key", "code", "signature", "x-amz-signature",
})
_URL_QUERY_RE = re.compile(
    r"([?&])(" + "|".join(_SENSITIVE_QUERY_PARAMS) + r")=([^&\s]+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 4. JWT / private keys (ported from hermes)
# ---------------------------------------------------------------------------
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----.*?-----END [^-]+-----",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# 5. Chinese PII (extension beyond hermes; our primary scenario)
# ---------------------------------------------------------------------------
# 18-digit ID card (checksum char X allowed).
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# Mainland mobile: 1[3-9]xxxxxxxxx, not preceded/followed by digits.
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# Bank cards 16-19 digits (runs of digits not caught as ID/phone).
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
# Landlines with area code: 010-1234567 / 0755-12345678
_LANDLINE = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")
# Email addresses.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# ---------------------------------------------------------------------------
# masking helpers
# ---------------------------------------------------------------------------

def _mask_token(token: str) -> str:
    """Hermes-style: short tokens fully masked, long ones keep head/tail."""
    if len(token) < 18:
        return "***"
    return f"{token[:6]}***{token[-4:]}"


def _mask_pii(match: re.Match) -> str:
    s = match.group()
    if len(s) <= 4:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


def _mask_full(match: re.Match) -> str:
    return "[REDACTED]"


def _mask_url_query(match: re.Match) -> str:
    return f"{match.group(1)}{match.group(2)}=***"


def _mask_env_value(match: re.Match) -> str:
    return f"{match.group(1)}={_mask_token(match.group(3))}"


# pre-check gates (hermes perf pattern: skip regex when substring absent)
def _has_secret_hint(text: str) -> bool:
    """True when text plausibly contains a credential to redact."""
    lowered = text.lower()
    if any(w in lowered for w in (
            "key", "token", "secret", "password", "passwd", "auth",
            "credential", "jwt", "bearer")):
        return True
    if "://" in text:  # URL: query-param / userinfo redaction applies
        return True
    if "-----BEGIN" in text or "eyJ" in text:  # private key / JWT
        return True
    return any(s in text for s in _PREFIX_SUBSTRINGS)


def _has_pii_hint(text: str) -> bool:
    """Cheap pre-check for digit-based PII (ID/phone/bank/landline)."""
    return any(len(run) >= 4 for run in re.findall(r"\d{4,}", text))


def _has_email_hint(text: str) -> bool:
    return "@" in text and "." in text


def desensitize(text: str) -> str:
    """Mask credentials and PII before text leaves the machine (to the LLM).

    Non-matching text passes through unchanged.
    """
    if not text:
        return text

    out = text

    # --- credentials (gated) ---
    if _has_secret_hint(out):
        for pattern in _PREFIX_RES:
            out = pattern.sub(lambda m: _mask_token(m.group()), out)
        out = _JWT_RE.sub("[REDACTED-JWT]", out)
        out = _PRIVATE_KEY_RE.sub("[REDACTED-PRIVATE-KEY]", out)
        out = _URL_QUERY_RE.sub(_mask_url_query, out)
        out = _ENV_ASSIGN_RE.sub(_mask_env_value, out)

    # --- PII (gated) ---
    if _has_pii_hint(out):
        out = _ID_CARD.sub(_mask_pii, out)
        out = _PHONE.sub(_mask_pii, out)
        out = _LANDLINE.sub(_mask_pii, out)
        # Bank card last: it overlaps ID/phone ranges; only surviving long
        # digit runs get masked.
        out = _BANK_CARD.sub(_mask_pii, out)

    # --- email (own gate: no digits required) ---
    if _has_email_hint(out):
        out = _EMAIL.sub(
            lambda m: m.group().split("@")[0][:2] + "***@" + m.group().split("@")[1],
            out,
        )

    return out
