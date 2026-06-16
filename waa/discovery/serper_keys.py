"""
Serper API key rotation (multi-key pool).

Serper's free tier caps queries per key (~2500/month, which the campaign spends
fast). Rather than stop at one key's limit, we hold a POOL of keys and roll to
the next one the moment a key reports it's out of credits. Add more keys and the
daily ceiling rises linearly — no code change.

Keys are loaded from the environment (never hard-coded / committed):
    SERPER_API_KEY            one key, or several comma-separated
    SERPER_API_KEY_2 .. _N    additional keys, one per variable

SOLID:
- `SerperKeyPool` has one job: hand out a usable key and remember which ones
  are spent (SRP). It knows nothing about HTTP — the caller reports exhaustion
  back via `mark_exhausted()` (DIP), so it is trivially unit-tested.
- The process-global pool (get_serper_pool) keeps the "spent" state across all
  queries in a run; reset_serper_pool() gives tests a clean slate.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence


def load_serper_keys() -> list[str]:
    """Collect Serper keys from env, in order, de-duplicated.

    Accepts a comma-separated SERPER_API_KEY plus numbered SERPER_API_KEY_2..N.
    """
    raw: list[str] = []
    raw.extend((os.getenv("SERPER_API_KEY", "") or "").split(","))
    i = 2
    while True:
        val = os.getenv(f"SERPER_API_KEY_{i}")
        if not val:
            break
        raw.extend(val.split(","))
        i += 1
    # strip, drop empties, preserve first-seen order, de-dupe
    seen: set[str] = set()
    keys: list[str] = []
    for k in (s.strip() for s in raw):
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


class SerperKeyPool:
    """Hands out the current usable key; rotates past exhausted ones."""

    def __init__(self, keys: Sequence[str]) -> None:
        # de-dupe defensively while preserving order
        self._keys: list[str] = list(dict.fromkeys(k for k in keys if k))
        self._exhausted: set[str] = set()

    def current(self) -> Optional[str]:
        """The first key that still has credit, or None when all are spent."""
        for k in self._keys:
            if k not in self._exhausted:
                return k
        return None

    def mark_exhausted(self, key: str) -> None:
        """Record that `key` is out of credits; current() moves to the next."""
        if key:
            self._exhausted.add(key)

    def has_key(self) -> bool:
        return self.current() is not None

    def total(self) -> int:
        return len(self._keys)

    def active(self) -> int:
        return sum(1 for k in self._keys if k not in self._exhausted)

    def status(self) -> str:
        return f"{self.active()}/{self.total()} Serper key(s) with credit"


# ---------------------------------------------------------------------------
# Process-global pool
# ---------------------------------------------------------------------------

_POOL: Optional[SerperKeyPool] = None


def get_serper_pool() -> SerperKeyPool:
    global _POOL
    if _POOL is None:
        _POOL = SerperKeyPool(load_serper_keys())
    return _POOL


def reset_serper_pool() -> SerperKeyPool:
    """Rebuild the pool from the current environment (used by tests / new run)."""
    global _POOL
    _POOL = SerperKeyPool(load_serper_keys())
    return _POOL
