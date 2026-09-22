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
    name: str              # "search" | "help" | "list" | "pick" | ...
    query: Optional[ParsedQuery] = None
    pick_index: Optional[int] = None
    raw: str = ""


class CommandRouter:
    """Decides what a slash command means and builds a Command."""

    _FIXED = {"help", "list", "stats", "delete"}

    def __init__(self, cfg: Config):
        self._cfg = cfg

    def route(self, text: str) -> Command:
        """Parse a ``/...`` message into a Command."""
        body = text.lstrip("/").strip()
        if not body:
            return Command(name="help", raw=text)

        # /<n> -> pick a result from a previous search
        if body.isdigit():
            return Command(name="pick", pick_index=int(body), raw=text)

        # /help /list /stats /delete
        head = body.split()[0].lower()
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
