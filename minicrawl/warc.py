"""Stage 11 — WARC: the bytes as they arrived.

A content store keeps documents. An ARCHIVE keeps *responses* — the status
line, the headers, the body, in a format another tool can replay years later
without knowing anything about this crawler. That format is WARC (ISO 28500),
and the Internet Archive, Common Crawl and every web archive you have ever used
are piles of it.

The format is simpler than its reputation. A file is a concatenation of
records, each one:

    WARC/1.1
    WARC-Type: response
    WARC-Record-ID: <urn:uuid:...>
    WARC-Date: 2026-09-04T10:00:00Z
    WARC-Target-URI: http://example.com/a
    Content-Type: application/http;msgtype=response
    Content-Length: 1234
                                    <- blank line
    HTTP/1.1 200 OK                 <- the block: a whole HTTP response,
    Content-Type: text/html            headers and all
                                    <- blank line
    <!doctype html>...
                                    <- two blank lines end the record

Each record is gzipped as its own member and the members are concatenated. A
gzip decoder reads that as one stream, and a tool that knows the trick can seek
to any record without decompressing what came before. That is the whole reason
WARC files are usable at a hundred gigabytes.

THE PROBLEM THIS FILE EXISTS TO SOLVE

httpx decodes `Content-Encoding: gzip` transparently. So the body we hold is
the decoded entity while the headers still describe the compressed one, and
`Content-Length` is the compressed length. Write that pair into a record and
every reader either rejects it or reads the wrong number of bytes.

A true archival crawler keeps the wire bytes and the headers stay honest. This
one does not have them -- by the time `fetch` returns, the decoding has already
happened -- so it reconciles in the other direction: drop `Content-Encoding`,
rewrite `Content-Length` to what is actually there, and record that it did.

The record is then replayable and self-consistent, but it is no longer
byte-exact provenance: you cannot prove from the archive what the server put on
the wire. For a search corpus that is irrelevant. For evidence it is
disqualifying, and knowing which of those you are building is the point.
"""
from __future__ import annotations

import gzip
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

WARC_VERSION = b"WARC/1.1"
CRLF = b"\r\n"
SOFTWARE = "minicrawl/0.1 (https://example.invalid/minicrawl)"

# Headers that describe a transfer we have already undone, or a connection that
# no longer exists. Carrying them into a record makes it describe bytes that
# are not in it.
DROP_HEADERS = {b"content-encoding", b"transfer-encoding", b"connection",
                b"keep-alive", b"content-length"}


def warc_date(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_id() -> str:
    return f"<urn:uuid:{uuid.uuid4()}>"


def sha256_field(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def http_block(fetched) -> bytes:
    """Rebuild the HTTP response: status line, headers, blank line, body.

    Headers keep their original order and casing, which is why `fetch` holds on
    to `raw_headers` — the lowercased dict everything else uses would silently
    normalise an archive.
    """
    status_line = (f"{fetched.http_version} {fetched.status} "
                   f"{fetched.reason_phrase}".rstrip()).encode("latin-1")
    lines = [status_line]
    for name, value in fetched.raw_headers:
        if name.lower() in DROP_HEADERS:
            continue
        lines.append(name + b": " + value)
    # Content-Length is re-derived, never copied: the body may have been
    # decompressed since the server measured it.
    lines.append(b"Content-Length: " + str(len(fetched.body)).encode())
    return CRLF.join(lines) + CRLF + CRLF + fetched.body


class WarcWriter:
    """Append gzip-member WARC records to a file.

    Opened lazily so that configuring a writer costs nothing until something is
    actually archived, and so an empty crawl leaves no empty file.
    """

    def __init__(self, path: str | Path, software: str = SOFTWARE):
        self.path = Path(path)
        self.software = software
        self._handle = None
        self.records = 0
        self.bytes_written = 0
        self.refusals: dict[str, int] = {}

    # -- record framing ----------------------------------------------------
    def _open(self):
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("ab")
            self._write_record(b"warcinfo", {
                b"WARC-Filename": self.path.name.encode(),
                b"Content-Type": b"application/warc-fields",
            }, (f"software: {self.software}\r\n"
                "format: WARC File Format 1.1\r\n").encode())
        return self._handle

    def _write_record(self, warc_type: bytes, headers: dict[bytes, bytes],
                      block: bytes) -> None:
        head = [WARC_VERSION,
                b"WARC-Type: " + warc_type,
                b"WARC-Record-ID: " + record_id().encode(),
                b"WARC-Date: " + warc_date().encode(),
                b"WARC-Block-Digest: " + sha256_field(block).encode()]
        for name, value in headers.items():
            head.append(name + b": " + value)
        head.append(b"Content-Length: " + str(len(block)).encode())
        # Two CRLFs after the block terminate the record — a reader uses them
        # to confirm it landed where the Content-Length said it would.
        raw = CRLF.join(head) + CRLF + CRLF + block + CRLF + CRLF

        # One gzip member per record, so the file is seekable record by record.
        member = gzip.compress(raw, mtime=0)
        handle = self._handle if warc_type == b"warcinfo" else self._open()
        handle.write(member)
        self.records += 1
        self.bytes_written += len(member)

    # -- the public surface ------------------------------------------------
    def write_response(self, fetched) -> str | None:
        """Archive one response. Returns the reason it was refused, or None."""
        if fetched.not_modified:
            return self._refuse("not_modified")     # no body to archive
        if fetched.error or not fetched.ok:
            return self._refuse("not_ok")
        if fetched.truncated:
            # A capped body written under a Content-Length we invented is a
            # record that claims to hold a document and holds a prefix.
            return self._refuse("truncated")

        self._open()
        block = http_block(fetched)
        headers = {
            b"WARC-Target-URI": fetched.final_url.encode(),
            b"WARC-Payload-Digest": sha256_field(fetched.body).encode(),
            b"Content-Type": b"application/http;msgtype=response",
        }
        if fetched.content_encoded:
            # Say so in the record rather than leaving a reader to wonder why
            # the bytes do not match what the origin claimed to send. A custom
            # field is legal in WARC and is the honest place for it.
            headers[b"X-Minicrawl-Decoded"] = fetched.headers[
                "content-encoding"].encode()
        self._write_record(b"response", headers, block)
        return None

    def _refuse(self, reason: str) -> str:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        return reason

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
