"""Link snapshot fetching (DESIGN.md §7).

Grabs title/description/body-excerpt once at ingest time so the link's
content survives URL rot and is searchable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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

    def fetch(self, url: str) -> LinkSnapshot:
        """Fetch the URL, parse title/meta/body via requests + bs4."""
        # TODO(impl): GET with UA, parse og:title/og:description/body text,
        #   truncate body_excerpt; on failure return status="error"
        #   with just the url.
        raise NotImplementedError
