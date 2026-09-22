"""Raw storage: writes each message as a directory under raw/ (DESIGN.md §5.1).

The raw layer is immutable — files written here are never modified.  Derived
data (extracted text, metadata) lives elsewhere and can be rebuilt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..config import Config
from ..models import Message


class RawStore:
    """Manages raw/<msg_id>/ directories."""

    def __init__(self, cfg: Config):
        self._root = cfg.raw_dir

    def message_dir(self, msg_id: str) -> Path:
        return self._root / msg_id

    def save_message(self, message: Message) -> Path:
        """Create the message directory, write message.json and any raw
        attachment files.  Returns the directory path."""
        d = self.message_dir(message.msg_id)
        d.mkdir(parents=True, exist_ok=True)
        # TODO(impl): write message.json (metadata + raw structure),
        #   write each attachment's original file bytes into d/,
        #   link snapshot json, content.txt for text.
        raise NotImplementedError

    def load_raw_structure(self, msg_id: str) -> Dict[str, Any]:
        """Read back message.json for a stored message."""
        raise NotImplementedError
