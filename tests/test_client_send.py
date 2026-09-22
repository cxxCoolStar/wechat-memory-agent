"""Tests for the outbound file path (item builder + crypto roundtrip)."""

import base64

from wechat_memory.weixin import crypto
from wechat_memory.weixin.client import WeixinClient


class TestOutboundMediaItem:
    def test_image(self):
        item = WeixinClient._outbound_media_item(
            "a.jpg", "EQP", "B64KEY", 100, 90, "a.jpg", "md5")
        assert item["type"] == 2
        assert item["image_item"]["mid_size"] == 100
        assert item["image_item"]["media"]["encrypt_query_param"] == "EQP"

    def test_video(self):
        item = WeixinClient._outbound_media_item(
            "a.mp4", "EQP", "B64KEY", 100, 90, "a.mp4", "md5")
        assert item["type"] == 5
        assert item["video_item"]["video_md5"] == "md5"

    def test_generic_file(self):
        item = WeixinClient._outbound_media_item(
            "a.pdf", "EQP", "B64KEY", 100, 90, "a.pdf", "md5")
        assert item["type"] == 4
        assert item["file_item"]["file_name"] == "a.pdf"
        assert item["file_item"]["len"] == "90"


class TestAesRoundtrip:
    """Encrypt/decrypt roundtrip used by the upload path."""

    def test_roundtrip(self):
        key = bytes(range(16))
        data = b"x" * 1000
        assert crypto.aes128_ecb_decrypt(
            crypto.aes128_ecb_encrypt(data, key), key) == data

    def test_parse_aes_key_hex_form(self):
        """base64(hex_string) is the API form (grey-box pitfall)."""
        raw = bytes(range(16))
        b64hex = base64.b64encode(raw.hex().encode()).decode()
        assert crypto.parse_aes_key(b64hex) == raw

    def test_aes_padded_size(self):
        assert crypto.aes_padded_size(16) == 32
        assert crypto.aes_padded_size(17) == 32
        assert crypto.aes_padded_size(31) == 32
        assert crypto.aes_padded_size(32) == 48
