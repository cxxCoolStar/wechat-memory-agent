"""Slash-command router (DESIGN.md §10).

``/`` + fixed word  -> fixed command (help/list/...)
``/`` + other       -> natural-language query (or ``/<n>`` to pick a result)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import Config
from ..search.query_parser import ParsedQuery, parse_query


@dataclass
class Command:
    name: str              # "search" | "help" | "list" | "pick" | "delete" | ...
    query: Optional[ParsedQuery] = None
    pick_index: Optional[int] = None
    raw: str = ""


class CommandRouter:
    """Decides what a slash command means and builds a Command."""

    _FIXED = {"help", "list", "stats", "delete", "undo", "trash"}

    def __init__(self, cfg: Config):
        self._cfg = cfg

    def route(self, text: str) -> Command:
        """Parse a ``/...`` message into a Command."""
        body = text.lstrip("/").strip()
        if not body:
            return Command(name="help", raw=text)

        # /del N — delete entry N from the last search listing
        low = body.lower()
        if low.startswith("del ") and body[4:].strip().isdigit():
            return Command(name="delete", pick_index=int(body[4:].strip()),
                           raw=text)

        # /<n> — pick a result; "/<n> 删除" deletes it instead
        if body[0].isdigit():
            rest = body.split(maxsplit=1)
            index = int(rest[0])
            if len(rest) > 1 and "删除" in rest[1]:
                return Command(name="delete", pick_index=index, raw=text)
            return Command(name="pick", pick_index=index, raw=text)

        # /help /list /stats /undo /trash
        head = low.split()[0]
        if head in self._FIXED:
            return Command(name=head, raw=text)

        # Everything else is a natural-language search query.
        # Rule-based parse first; caller may fall back to LLM when
        # keywords are empty but the text was clearly a search intent.
        return Command(
            name="search",
            query=parse_query(body, self._cfg),
            raw=text,
        )
