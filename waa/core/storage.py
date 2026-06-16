"""
Shared persistence and URL helpers.

Single responsibility: small, well-tested utilities that several modules
were each re-implementing. Two things live here:

  domain_of(url)  — normalised registrable host (lowercased, no "www.")
  JsonStore       — load-or-default + atomic save for a JSON file with a
                    known default shape

Corruption policy is explicit per store, because it differs by design:
  - Campaign progress / sent registry  → tolerate_corrupt=False (raise).
    Silently losing this state means re-emailing people we already
    contacted. Better to fail loud and let a human look.
  - Replies-seen dedup cache           → tolerate_corrupt=True (reset).
    It's re-derivable from the mailbox; a corrupt cache should never block
    the monitor.
"""

from __future__ import annotations

import json
import os
from typing import Callable
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def domain_of(url: str) -> str:
    """
    Return the normalised host for `url`: lowercased, with a leading
    "www." stripped. Tolerates a bare domain or a missing scheme.

        domain_of("https://www.Example.com/path")  -> "example.com"
        domain_of("example.com")                     -> "example.com"
        domain_of("")                                 -> ""
    """
    if not url:
        return ""
    netloc = urlparse(url).netloc or urlparse("//" + url).netloc or url
    host = netloc.lower().strip()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# JSON file store
# ---------------------------------------------------------------------------

class JsonStore:
    """
    A JSON file persisted with a known default shape.

    The path is captured at construction time, so callers that reassign a
    module-level path variable between calls should construct a fresh store
    each call (the wrapper functions in audit_agent / replies_monitor do
    exactly this so test patching of the path keeps working).
    """

    def __init__(
        self,
        path: str,
        default_factory: Callable[[], object],
        *,
        tolerate_corrupt: bool = False,
    ) -> None:
        self._path = path
        self._default_factory = default_factory
        self._tolerate_corrupt = tolerate_corrupt

    def load(self) -> object:
        """Return the parsed JSON, or a fresh default if the file is absent
        (or corrupt, when tolerate_corrupt=True)."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return self._default_factory()
        except json.JSONDecodeError:
            if self._tolerate_corrupt:
                return self._default_factory()
            raise

    def save(self, data: object) -> None:
        """Write `data` as indented UTF-8 JSON, creating the parent dir."""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
