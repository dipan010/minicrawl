"""Stage 5 — a frontier that survives the process.

Everything before this stage loses the entire crawl if anything goes wrong at
page 40,000. That is fine for a corpus of nineteen pages and useless for a real
one, where crawls run for days and the interesting failures are the ones that
happen partway through.

The table *is* the seen-set: `url` is the primary key, so `INSERT OR IGNORE`
does deduplication and persistence in the same statement.

    state = queued     waiting to be handed to a worker
          | in_flight  handed out, not yet finished
          | done       finished, never to be queued again

Recovery is one statement at open: any row still marked `in_flight` belongs to
a worker that no longer exists, so it goes back to `queued`. Without it, every
crash silently drops exactly the requests that were in progress — the ones
most likely to have been slow or difficult.

`sqlite3` is synchronous and these calls block the event loop. At this scale
each one is microseconds against a local file, which is cheaper than the thread
hop that avoiding it would cost. Stage 9 moves the frontier out of the process
entirely and the question stops being ours.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from ..politeness import Politeness
from .base import Request
from .scheduling import SchedulingFrontier, host_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier (
    url      TEXT PRIMARY KEY,
    host     TEXT NOT NULL,
    depth    INTEGER NOT NULL,
    via      TEXT,
    state    TEXT NOT NULL DEFAULT 'queued',
    added_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS frontier_ready ON frontier(state, host, added_at);
"""


class SqliteFrontier(SchedulingFrontier):
    def __init__(self, politeness: Politeness, path: str | Path):
        super().__init__(politeness)
        self.path = str(path)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")     # survives an abrupt exit
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self.recovered = self._recover()
        self._pending = self._count("queued")

    def _recover(self) -> int:
        """Requeue anything a dead worker was holding."""
        cursor = self._db.execute(
            "UPDATE frontier SET state='queued' WHERE state='in_flight'")
        return cursor.rowcount or 0

    def _count(self, state: str) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM frontier WHERE state=?", (state,)).fetchone()[0]

    # -- storage hooks -----------------------------------------------------
    def _add(self, request: Request) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO frontier(url, host, depth, via, state, added_at)"
            " VALUES (?,?,?,?,'queued',?)",
            (request.url, host_of(request.url), request.depth, request.via, time.time()))
        return cursor.rowcount == 1     # 0 means the URL was already known

    def _take(self, host: str) -> Request | None:
        row = self._db.execute(
            "SELECT url, depth, via FROM frontier"
            " WHERE state='queued' AND host=? ORDER BY added_at LIMIT 1",
            (host,)).fetchone()
        if row is None:
            return None
        self._db.execute("UPDATE frontier SET state='in_flight' WHERE url=?", (row[0],))
        return Request(url=row[0], depth=row[1], via=row[2])

    def _hosts_with_work(self) -> Iterable[str]:
        return [r[0] for r in self._db.execute(
            "SELECT DISTINCT host FROM frontier WHERE state='queued'")]

    def known(self, url: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM frontier WHERE url=?", (url,)).fetchone() is not None

    def _mark_seen(self, url: str) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO frontier(url, host, depth, via, state, added_at)"
            " VALUES (?,?,0,NULL,'done',?)", (url, host_of(url), time.time()))
        return cursor.rowcount == 1

    def _requeue(self, request: Request) -> None:
        self._db.execute("UPDATE frontier SET state='queued' WHERE url=?",
                         (request.url,))

    def _complete(self, url: str) -> None:
        self._db.execute("UPDATE frontier SET state='done' WHERE url=?", (url,))

    @property
    def seen_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]

    @property
    def done_count(self) -> int:
        return self._count("done")

    @property
    def host_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(DISTINCT host) FROM frontier").fetchone()[0]

    def close_db(self) -> None:
        self._db.close()
