"""AES-128-ECB crypto for WeChat iLink media (ported from hermes weixin.py).

Media files travel through an encrypted CDN: download ciphertext, decrypt
with the per-media AES key (base64(hex)); uploads encrypt with a fresh key.
"""

from __future__ import annotations

import base64
from typing import Optional


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if 1 <= pad_len <= 16 and data[-pad_len:] == bytes([pad_len]) * pad_len:
        return data[:-pad_len]
    return data  # tolerate bad padding


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB, stripping PKCS7 padding."""
    # TODO(impl): from cryptography.hazmat... Cipher(algorithms.AES(key), modes.ECB())
    raise NotImplementedError


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt AES-128-ECB with PKCS7 padding."""
    raise NotImplementedError


def parse_aes_key(aes_key_b64: str) -> bytes:
    """Decode a base64(hex) AES key from the media metadata into raw bytes."""
    raw = base64.b64decode(aes_key_b64)
    if len(raw) == 16:
        return raw
    if len(raw) == 32:
        try:
            text = raw.decode("ascii")
            if all(c in "0123456789abcdefABCDEF" for c in text):
                return bytes.fromhex(text)
        except UnicodeDecodeError:
            pass
    raise ValueError("unsupported aes key length")
