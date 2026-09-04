"""Stage 11 — what a crawl leaves behind.

Ten stages of crawler and not one byte kept. Everything so far fetched a page,
classified it, counted it and dropped it, which is fine for proving the crawl
is correct and useless for anything you would actually crawl *for*.

"Store the pages" is three different jobs wearing one name:

  an archive        the bytes exactly as they arrived, replayable  -> warc.py
  a corpus          one copy of each distinct document             -> here
  an index          extracted text and metadata, queryable         -> here

This module does the middle two. The organising idea is CONTENT ADDRESSING:
name each object by the SHA-256 of its bytes, and deduplication stops being a
feature you implement and becomes a property of the naming scheme. Two URLs
that serve identical bytes write to the same path, and the second write is a
no-op that costs a hash.

That matters more than it sounds. The corpus alone reaches /a by three routes
(/a, /a/, and the end of the /r/1 redirect chain), and /dup/exact-1 and
/dup/exact-2 are byte-identical by declaration. A store keyed on URL keeps five
copies of two documents. A store keyed on content keeps two.

Two things must never be stored, and both look like nothing when they go wrong:

  a 304 response    has no body. Storing it overwrites the real object with
                    zero bytes, and with --freshness a second crawl is mostly
                    304s -- so run two silently empties the archive.
  a truncated body  is not the document. Storing it under a digest of the
                    fragment claims a completeness the bytes do not have.

Both are refusals with a stated reason rather than exceptions, for the same
reason fetch errors are data: the crawl needs to record that it declined.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    digest       TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    content_type TEXT,
    stored_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    url          TEXT PRIMARY KEY,
    final_url    TEXT NOT NULL,
    digest       TEXT NOT NULL REFERENCES objects(digest),
    status       INTEGER,
    title        TEXT,
    verdict      TEXT,
    fetched_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS records_digest ON records(digest);
"""


@dataclass(slots=True)
class Stored:
    """The outcome of offering one response to a store."""
    digest: str | None
    new_object: bool = False
    refused: str | None = None      # why nothing was written

    @property
    def ok(self) -> bool:
        return self.refused is None


class Store(Protocol):
    def put(self, fetched, *, title: str = "", verdict: str = "") -> Stored: ...

    def get(self, url: str) -> bytes | None: ...

    def close(self) -> None: ...


class NullStore:
    """Stores nothing. The behaviour of every stage before this one, named."""

    def put(self, fetched, *, title: str = "", verdict: str = "") -> Stored:
        return Stored(digest=None, refused="no store configured")

    def get(self, url: str) -> bytes | None:
        return None

    def close(self) -> None:
        pass


def digest_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class ContentStore:
    """Objects on disk under their own hash; an index in SQLite beside them.

        <root>/index.sqlite3
        <root>/objects/ab/abcdef...        one file per distinct body

    The two-character prefix directory is not decoration: a flat directory of
    a million files is slow to list on every filesystem worth naming, and the
    fan-out costs one string slice.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.root / "index.sqlite3", isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self.bytes_written = 0
        self.bytes_deduplicated = 0
        self.refusals: dict[str, int] = {}

    # -- writing -----------------------------------------------------------
    def path_for(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def put(self, fetched, *, title: str = "", verdict: str = "") -> Stored:
        if fetched.not_modified:
            # No body arrived. Writing it would replace the stored document
            # with nothing, and a --freshness crawl is mostly 304s.
            return self._refuse("not_modified")
        if fetched.error or not fetched.ok:
            return self._refuse("not_ok")
        if fetched.truncated:
            # A digest of a fragment claims a completeness the bytes lack.
            return self._refuse("truncated")

        digest = digest_of(fetched.body)
        path = self.path_for(digest)
        new_object = not path.exists()
        if new_object:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fetched.body)
            self.bytes_written += len(fetched.body)
            self._db.execute(
                "INSERT OR IGNORE INTO objects(digest, size, content_type, stored_at)"
                " VALUES (?,?,?,?)",
                (digest, len(fetched.body), fetched.content_type, time.time()))
        else:
            # The bytes were already here under this exact name. Deduplication
            # is not a step that ran; it is what content addressing means.
            self.bytes_deduplicated += len(fetched.body)

        self._db.execute(
            "INSERT INTO records(url, final_url, digest, status, title, verdict, fetched_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url,"
            " digest=excluded.digest, status=excluded.status, title=excluded.title,"
            " verdict=excluded.verdict, fetched_at=excluded.fetched_at",
            (fetched.url, fetched.final_url, digest, fetched.status, title,
             verdict, time.time()))
        return Stored(digest=digest, new_object=new_object)

    def _refuse(self, reason: str) -> Stored:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        return Stored(digest=None, refused=reason)

    # -- reading -----------------------------------------------------------
    def get(self, url: str) -> bytes | None:
        row = self._db.execute("SELECT digest FROM records WHERE url=?", (url,)).fetchone()
        return self.read(row[0]) if row else None

    def read(self, digest: str) -> bytes | None:
        path = self.path_for(digest)
        return path.read_bytes() if path.exists() else None

    def urls_for(self, digest: str) -> list[str]:
        """Every URL that served these exact bytes."""
        return [r[0] for r in self._db.execute(
            "SELECT url FROM records WHERE digest=? ORDER BY url", (digest,))]

    # -- counting ----------------------------------------------------------
    @property
    def record_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    @property
    def object_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]

    def duplicate_groups(self) -> list[tuple[str, int]]:
        """Digests served by more than one URL, commonest first."""
        return [(r[0], r[1]) for r in self._db.execute(
            "SELECT digest, COUNT(*) c FROM records GROUP BY digest"
            " HAVING c > 1 ORDER BY c DESC")]

    def close(self) -> None:
        self._db.close()
