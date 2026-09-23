"""Regression tests for file type detection (magic bytes > extension)."""

from pathlib import Path

import pytest

from wechat_memory.ingest.classifier import subtype_to_level1, should_ocr
from wechat_memory.utils.type_detect import detect_type

# Real sample files from the user's WeChat temp dir (observed during dev).
SAMPLES = Path(
    r"C:/Users/asta1/xwechat_files/wxid_gc9ooxdzcgnb22_a7a0/temp/RWTemp"
)


def _sample(*parts: str) -> Path:
    p = SAMPLES.joinpath(*parts)
    return p if p.exists() else None


class TestMagicByteDetection:
    def test_real_pdf_invoice(self):
        """The invoice PDF must be detected by magic bytes, not extension."""
        p = _sample(
            "2026-08/9e20f478899dc29eb19741386f9343c8",
            "dzfp_26442000009628529551_广州市白云区嘉禾名扬煮艺杨记火锅店_20260822154402(1).pdf",
        )
        if p is None:
            pytest.skip("sample file not available")
        assert detect_type(p) == "pdf"

    def test_real_screenshot_jpg(self):
        p = _sample(
            "2026-09/9e20f478899dc29eb19741386f9343c8",
            "89ff6a054cda4f038a03f9fa7fee3341.jpg",
        )
        if p is None:
            pytest.skip("sample file not available")
        assert detect_type(p) == "screenshot"

    def test_real_spring_boot_jar(self):
        """224MB Spring Boot jar: PK magic -> MANIFEST.MF -> jar."""
        p = _sample(
            "2026-09/9e20f478899dc29eb19741386f9343c8",
            "iam-console(3).jar",
        )
        if p is None:
            pytest.skip("sample file not available")
        assert detect_type(p) == "jar"

    def test_real_source_zip(self):
        p = _sample(
            "2026-09/9e20f478899dc29eb19741386f9343c8",
            "wx-cli-again-main.zip",
        )
        if p is None:
            pytest.skip("sample file not available")
        assert detect_type(p) == "archive"


class TestSubtypeMapping:
    @pytest.mark.parametrize("subtype,level1", [
        ("pdf", "document"),
        ("docx", "document"),
        ("xlsx", "document"),
        ("screenshot", "image"),
        ("photo", "image"),
        ("jar", "binary"),
        ("exe", "binary"),
        ("archive", "binary"),
        ("unknown", "other"),
    ])
    def test_mapping(self, subtype, level1):
        assert subtype_to_level1(subtype) == level1

    def test_screenshot_should_ocr(self):
        assert should_ocr("screenshot") is True


class TestFakeFiles:
    def test_fake_extension_txt_content_is_pdf(self, tmp_path):
        """A .txt file whose content is actually a PDF must detect as pdf."""
        p = tmp_path / "fake.txt"
        p.write_bytes(b"%PDF-1.7\n%fake pdf content")
        assert detect_type(p) == "pdf"

    def test_fake_extension_pdf_content_is_jpeg(self, tmp_path):
        """An image renamed to .pdf must be caught by magic bytes."""
        p = tmp_path / "fake.pdf"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        assert detect_type(p) == "screenshot"

    def test_unknown_binary(self, tmp_path):
        p = tmp_path / "mystery.bin"
        p.write_bytes(b"\x00\x01\x02\x03" * 8)
        assert detect_type(p) == "unknown"

    def test_docx_zip_container(self, tmp_path):
        """A docx is a PK zip with word/ inside."""
        import zipfile
        p = tmp_path / "doc.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", "<doc/>")
        assert detect_type(p) == "docx"

    def test_xlsx_zip_container(self, tmp_path):
        import zipfile
        p = tmp_path / "sheet.xlsx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("xl/workbook.xml", "<wb/>")
        assert detect_type(p) == "xlsx"


class TestVoiceSilk:
    """WeChat voice messages arrive as silk audio (#!SILK magic)."""

    def test_silk_magic(self, tmp_path):
        p = tmp_path / "v.silk"
        p.write_bytes(b"#!SILK_V3" + b"\x00" * 32)
        assert detect_type(p) == "voice"

    def test_voice_is_binary_level1(self):
        assert subtype_to_level1("voice") == "binary"


class TestPlainTextFiles:
    """txt files have no magic bytes — extension or content heuristic only.
    (Regression: 资产情况.txt stored as subtype=unknown, never indexed.)"""

    def test_txt_extension(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("中文文本内容", encoding="utf-8")
        assert detect_type(p) == "text_plain"

    def test_md_and_csv(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("# 标题\n正文", encoding="utf-8")
        assert detect_type(p) == "text_plain"
        c = tmp_path / "a.csv"
        c.write_text("a,b\n1,2", encoding="utf-8")
        assert detect_type(c) == "text_plain"

    def test_extensionless_text(self, tmp_path):
        p = tmp_path / "noext"
        p.write_text("无后缀的中文文本\n第二行", encoding="utf-8")
        assert detect_type(p) == "text_plain"

    def test_extensionless_binary_not_text(self, tmp_path):
        p = tmp_path / "noext"
        p.write_bytes(b"\x00\x01\x02\x03abc" * 10)
        assert detect_type(p) == "unknown"

    def test_text_plain_maps_to_document(self):
        assert subtype_to_level1("text_plain") == "document"
