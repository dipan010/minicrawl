"""Stage 5 — noticing that a site is generating pages at you.

Normalisation cannot help here. Every URL in `/gen/1`, `/gen/2`, `/gen/3`… is
genuinely distinct and addresses a genuinely different page; there is no
canonical form that collapses them. The crawl is not confused, it is being fed.

What catches it is counting **shapes** rather than URLs: replace numeric path
segments with a placeholder and `/gen/1` and `/gen/9999` become the same
`/gen/<num>`. A budget per shape puts a ceiling on any single generated family
without capping the crawl as a whole, and without a rule that names `/gen`.

The other guards are cheaper and catch the classic shapes of the same problem:
calendars that link to next month forever (`max_path_depth`), symlink loops
that repeat a segment (`max_repeated_segment`), and faceted-search pages that
multiply query parameters (`max_query_params`).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .normalize import url_shape


@dataclass
class TrapGuard:
    max_path_depth: int = 12
    max_url_length: int = 2000
    max_query_params: int = 12
    max_repeated_segment: int = 3
    shape_budget: int = 5

    _shapes: Counter = field(default_factory=Counter, repr=False)
    rejected: Counter = field(default_factory=Counter, repr=False)

    def admit(self, url: str) -> str | None:
        """None if the URL may be queued, otherwise the reason it was refused."""
        reason = self._reason(url)
        if reason is not None:
            self.rejected[reason] += 1
        return reason

    def _reason(self, url: str) -> str | None:
        if len(url) > self.max_url_length:
            return "url_too_long"

        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) > self.max_path_depth:
            return "path_too_deep"
        if segments:
            most_common = Counter(segments).most_common(1)[0][1]
            if most_common > self.max_repeated_segment:
                return "repeated_segment"
        if parts.query and parts.query.count("&") + 1 > self.max_query_params:
            return "too_many_params"

        shape = url_shape(url)
        if self._shapes[shape] >= self.shape_budget:
            return "shape_budget"
        self._shapes[shape] += 1
        return None

    @property
    def shapes_seen(self) -> int:
        return len(self._shapes)

    def budget_used(self, shape: str) -> int:
        return self._shapes[shape]
