"""Stage 8 — deciding what to fetch again, and how to fetch it cheaply.

Two separate problems that are usually conflated:

  HOW to recheck   conditional GET. Send the validator the server gave you and
                   let it answer 304 with no body. Costs a round trip instead
                   of a page.

  WHEN to recheck  scheduling. Every page has its own rhythm: a homepage
                   changes hourly, an archived article never changes again.
                   Checking both on the same fixed interval wastes requests on
                   one and misses updates on the other.

The scheduling rule here is the classic adaptive one, and it is deliberately
simple enough to reason about: **unchanged doubles the interval, changed halves
it**, clamped at both ends. It converges on each page's actual rhythm without
storing history, and its failure modes are obvious — it is slow to react to a
page that suddenly starts changing, and it never checks a stable page more than
`max_interval` apart no matter what.

A validator is not proof. A 304 says the server believes nothing changed;
comparing content hashes on the responses you do fetch is what tells you
whether that belief is worth anything.

The store also keeps each page's OUTLINKS, which is not an optimisation. A 304
carries no body, so it carries no links — and a crawler that discovers only by
parsing responses goes blind the moment its cache starts working. Conditional
GET and link discovery are in direct tension, and remembering the link graph is
what resolves it. This was found by measuring: the first version of this stage
crawled 27 pages on the first run and 10 on the second.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MIN_INTERVAL = 60.0                     # never hammer, even for a live page
MAX_INTERVAL = 30 * 24 * 3600.0         # always look again eventually
INITIAL_INTERVAL = 3600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS freshness (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    content_hash  TEXT,
    last_checked  REAL NOT NULL,
    next_due      REAL NOT NULL,
    interval      REAL NOT NULL,
    checks        INTEGER NOT NULL DEFAULT 0,
    changes       INTEGER NOT NULL DEFAULT 0,
    body_bytes    INTEGER NOT NULL DEFAULT 0,
    links         TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS freshness_due ON freshness(next_due);
"""


@dataclass(slots=True)
class Record:
    url: str
    etag: str | None
    last_modified: str | None
    content_hash: str | None
    last_checked: float
    next_due: float
    interval: float
    checks: int
    changes: int
    body_bytes: int
    links: list[str]

    @property
    def change_rate(self) -> float:
        return self.changes / self.checks if self.checks else 0.0


class FreshnessStore:
    """Validators and schedules, persisted beside the frontier."""

    def __init__(self, path: str | Path | None = None,
                 min_interval: float = MIN_INTERVAL,
                 max_interval: float = MAX_INTERVAL,
                 initial_interval: float = INITIAL_INTERVAL):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.initial_interval = initial_interval
        self._db = sqlite3.connect(str(path) if path else ":memory:",
                                   isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self.conditional_hits = 0       # 304s: a recheck that cost no body
        self.bytes_saved = 0            # body bytes a 304 avoided sending
        self.revalidated = 0            # 200s where the content was unchanged anyway

    # -- reading -----------------------------------------------------------
    def get(self, url: str) -> Record | None:
        row = self._db.execute(
            "SELECT url, etag, last_modified, content_hash, last_checked, next_due,"
            " interval, checks, changes, body_bytes, links FROM freshness WHERE url=?",
            (url,)).fetchone()
        if row is None:
            return None
        return Record(*row[:-1], links=json.loads(row[-1]))

    def validators(self, url: str) -> dict[str, str]:
        """Headers that let the server answer 304 instead of sending the page."""
        record = self.get(url)
        if record is None:
            return {}
        headers = {}
        if record.etag:
            headers["If-None-Match"] = record.etag
        if record.last_modified:
            headers["If-Modified-Since"] = record.last_modified
        return headers

    def is_due(self, url: str, now: float | None = None) -> bool:
        record = self.get(url)
        return record is None or record.next_due <= (now or time.time())

    # -- writing -----------------------------------------------------------
    def record(self, url: str, *, etag: str | None = None,
               last_modified: str | None = None, content_hash: str | None = None,
               not_modified: bool = False, body_bytes: int = 0,
               links: list[str] | None = None, now: float | None = None) -> Record:
        now = now or time.time()
        previous = self.get(url)

        if not_modified:
            changed = False
            self.conditional_hits += 1
            self.bytes_saved += previous.body_bytes if previous else 0
            etag = etag or (previous.etag if previous else None)
            last_modified = last_modified or (previous.last_modified if previous else None)
            content_hash = previous.content_hash if previous else None
            body_bytes = previous.body_bytes if previous else 0
            links = previous.links if previous else []
        elif previous is None:
            changed = False             # first sight: nothing to compare against
        else:
            changed = (content_hash is not None
                       and previous.content_hash is not None
                       and content_hash != previous.content_hash)
            if not changed and previous.content_hash is not None:
                self.revalidated += 1

        interval = self._next_interval(previous, changed)
        checks = (previous.checks if previous else 0) + 1
        changes = (previous.changes if previous else 0) + (1 if changed else 0)

        self._db.execute(
            "INSERT INTO freshness(url, etag, last_modified, content_hash,"
            " last_checked, next_due, interval, checks, changes, body_bytes, links)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(url) DO UPDATE SET etag=excluded.etag,"
            " last_modified=excluded.last_modified, content_hash=excluded.content_hash,"
            " last_checked=excluded.last_checked, next_due=excluded.next_due,"
            " interval=excluded.interval, checks=excluded.checks,"
            " changes=excluded.changes, body_bytes=excluded.body_bytes,"
            " links=excluded.links",
            (url, etag, last_modified, content_hash, now, now + interval,
             interval, checks, changes, body_bytes, json.dumps(links or [])))
        return self.get(url)

    def _next_interval(self, previous: Record | None, changed: bool) -> float:
        if previous is None:
            return self.initial_interval
        interval = previous.interval / 2 if changed else previous.interval * 2
        return min(max(interval, self.min_interval), self.max_interval)

    def due_urls(self, now: float | None = None, limit: int = 1000) -> list[str]:
        return [r[0] for r in self._db.execute(
            "SELECT url FROM freshness WHERE next_due <= ? ORDER BY next_due LIMIT ?",
            (now or time.time(), limit))]

    @property
    def tracked(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM freshness").fetchone()[0]

    def close(self) -> None:
        self._db.close()
