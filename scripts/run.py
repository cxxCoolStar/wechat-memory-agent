"""Start the wechat-memory-agent.

Usage (from project root):
    .venv/Scripts/python scripts/run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wechat_memory.bot import main  # noqa: E402

if __name__ == "__main__":
    main()
