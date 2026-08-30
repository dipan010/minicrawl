"""The frontier interface — the seam the whole ladder turns on.

Stage 2 backs it with a deque, stage 5 with SQLite (so a crawl survives a
crash), stage 9 with Redis (so many workers share one queue). Nothing above
this interface changes when the backing store does; that is the point of
declaring it now rather than at stage 9.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Request:
    url: str
    depth: int = 0
    via: str | None = None          # the page that linked here, for debugging


class Frontier(Protocol):
    def push(self, request: Request) -> bool:
        """Enqueue unless already seen. Returns True if it was actually added."""

    def pop(self) -> Request | None:
        """Next request in the frontier's own order, or None when drained."""

    def __len__(self) -> int: ...

    @property
    def seen_count(self) -> int: ...
