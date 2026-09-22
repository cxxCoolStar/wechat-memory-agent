"""Link snapshot fetching (DESIGN.md §7).

Grabs title/description/body-excerpt once at ingest time so the link's
content survives URL rot and is searchable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}
_BODY_CAP = 4000  # chars of body excerpt to keep


@dataclass
class LinkSnapshot:
    url: str
    title: str = ""
    description: str = ""
    body_excerpt: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "ok"  # ok | error


class LinkFetcher:
    """Fetches a URL and extracts a snapshot."""

    def fetch(self, url: str, timeout: int = 30) -> LinkSnapshot:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            logger.warning("link fetch failed: %s (%s)", url, exc)
            return LinkSnapshot(url=url, status="error")

        soup = BeautifulSoup(resp.text, "lxml")
        snap = LinkSnapshot(url=url)

        # Title
        if soup.title and soup.title.string:
            snap.title = soup.title.string.strip()
        # Description (og:description or meta description)
        for meta in soup.find_all("meta"):
            prop = meta.get("property") or meta.get("name") or ""
            if prop.lower() in ("og:description", "description"):
                content = meta.get("content")
                if content:
                    snap.description = content.strip()
                    break
        # Body excerpt (article/main, else full text truncated)
        article = soup.select_one("article") or soup.select_one("main") or soup.body
        if article is not None:
            body = article.get_text(" ", strip=True)
            snap.body_excerpt = body[:_BODY_CAP]
        return snap
