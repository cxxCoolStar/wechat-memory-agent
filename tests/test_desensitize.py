"""Regression tests for desensitization (DESIGN.md §11.2).

Leaking any of these to the LLM is an incident.  Masking normal content
(false positive) is also a failure — it corrupts metadata quality.
"""

import pytest

from wechat_memory.utils.desensitize import desensitize


class TestCredentialsMasked:
    def test_vendor_prefixes(self):
        cases = [
            "我的key是sk-proj-abc123def456ghi789",
            "ark-4b33bf16-3127-4615-844d-13dc2856a800-b3485",
            "token: ghp_16CharactersXXXXXXXXXXXXXXXX",
        ]
        for t in cases:
            out = desensitize(t)
            assert t not in out, f"leaked: {t}"

    def test_url_query_params(self):
        t = "https://api.com/callback?code=abc123&state=xyz"
        out = desensitize(t)
        assert "abc123" not in out
        assert "state=xyz" in out          # non-sensitive param preserved

    def test_env_assignment(self):
        out = desensitize("OPENAI_API_KEY=sk-abc123def456ghi")
        assert "sk-abc123def456ghi" not in out
        assert "OPENAI_API_KEY=" in out    # key name preserved

    def test_jwt(self):
        out = desensitize("JWT: eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxMjN9.abcdef")
        assert "eyJhbGciOiJIUzI1NiJ9" not in out
        assert "[REDACTED-JWT]" in out

    def test_private_key(self):
        t = "-----BEGIN RSA PRIVATE KEY-----MIIEpAIBAAK-----END RSA PRIVATE KEY-----"
        out = desensitize(t)
        assert "MIIEpAIBAAK" not in out


class TestChinesePIIMasked:
    def test_phone(self):
        assert "13812345678" not in desensitize("手机号 13812345678")

    def test_id_card(self):
        assert "110101199001011234" not in desensitize("身份证 110101199001011234")

    def test_bank_card(self):
        assert "6222020200112233456" not in desensitize("银行卡 6222020200112233456")

    def test_landline(self):
        assert "0755-12345678" not in desensitize("座机 0755-12345678")

    def test_email(self):
        out = desensitize("邮箱 zhangsan@example.com")
        assert "zhangsan@example.com" not in out
        assert "@example.com" in out       # domain preserved


class TestNormalContentUntouched:
    """False positives corrupt metadata — these must pass through unchanged."""

    @pytest.mark.parametrize("text", [
        "发票金额 151.00 元，开票日期 2026年08月22日",
        "T-Mem 是一个记忆系统，EMNLP 2026 论文",
        "版本号 v1.2.3，共 1024 条数据",
        "电话是分机 8021",
        "访问 https://github.com/Sherlockwz/T-Mem 查看",
    ])
    def test_unchanged(self, text):
        assert desensitize(text) == text
