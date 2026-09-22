"""LLM-generated metadata (DESIGN.md §9.3).

Calls the OpenAI-compatible API to turn extracted text into structured
metadata (summary/keywords/category/entities/title).  Input is
desensitized before the request; output is parsed into ItemMetadata.
"""

from __future__ import annotations

from typing import Optional

from ..config import Config
from ..models import ItemMetadata
from ..utils.desensitize import desensitize


class LLMMetadataGenerator:
    def __init__(self, cfg: Config):
        self._cfg = cfg

    def generate(self, text: str, *, kind: str = "document") -> ItemMetadata:
        """Produce ItemMetadata for extracted text.

        kind: "document" | "image" | "link" | "invoice"
        """
        if not text.strip():
            return ItemMetadata()
        safe = desensitize(text)
        # TODO(impl): POST {base}/chat/completions with a JSON-mode prompt
        #   that asks for {"summary","keywords","category","entities","title"}
        #   and parse the JSON response into ItemMetadata.
        raise NotImplementedError

    def generate_invoice(self, text: str):
        """Extract structured invoice fields into InvoiceData."""
        raise NotImplementedError
