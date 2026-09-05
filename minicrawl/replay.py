"""Stage 12 — reading the archive back, with the origin switched off.

An archive you can write but not read is a backup nobody has ever restored.
This module closes the loop: given a WARC and its CDX index, pull one record
out by seeking to its offset, rebuild the HTTP response it holds, and hand the
body to the same `extract.parse` the live crawl used.

The test that makes this mean anything is the one where the corpus server is
DEAD. If the link graph re-derived from the archive matches the graph the live
crawl produced, then the archive genuinely holds the crawl, and every claim
stage 11 made about fidelity has been exercised rather than asserted.

TWO THINGS THE ARCHIVE'S OWN DESIGN FORCES ON A READER

  `/compressed` was archived DECODED, with `Content-Encoding` stripped and
  `Content-Length` re-derived (see warc.py). A reader that gunzips on sight
  would corrupt it. The record says what it did in `X-Minicrawl-Decoded`, and
  the correct behaviour is to trust the framing, not to guess from the body.

  `/volatile` changes on every request. It cannot round-trip against a fresh
  fetch, and excluding it belongs in how the comparison is built, not in an
  exception list bolted on after a test goes red.
"""
from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path

from .cdx import CdxIndex, CdxRecord
from .extract import parse

CRLF = b"\r\n"


@dataclass(slots=True)
class ArchivedResponse:
    """One response as the archive holds it."""
    url: str
    status: int
    reason_phrase: str
    http_version: str
    headers: dict[str, str]          # lowercased, as the rest of the crawler expects
    raw_headers: list[tuple[bytes, bytes]]
    body: bytes
    warc_headers: dict[str, str]

    @property
    def is_html(self) -> bool:
        return "html" in self.headers.get("content-type", "")

    @property
    def decoded_by_crawler(self) -> str | None:
        """What the crawler had already un-gzipped before archiving, if
        anything. A reader must NOT decompress a body this names."""
        return self.warc_headers.get("x-minicrawl-decoded")


def _split_headers(chunk: bytes) -> tuple[bytes, list[tuple[bytes, bytes]]]:
    lines = chunk.split(CRLF)
    return lines[0], [(n.strip(), v.strip())
                      for line in lines[1:] if line
                      for n, _, v in [line.partition(b":")]]


class WarcReader:
    """Seek to a record and read exactly it.

    This is where the one-gzip-member-per-record choice pays for itself: the
    bytes between `offset` and `offset + length` are a COMPLETE gzip stream, so
    they decompress on their own with nothing before them read.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.bytes_read = 0
        self.records_read = 0

    def read_at(self, offset: int, length: int) -> ArchivedResponse:
        with self.path.open("rb") as handle:
            handle.seek(offset)
            member = handle.read(length)
        self.bytes_read += len(member)
        self.records_read += 1
        return self._parse(gzip.decompress(member))

    def read(self, record: CdxRecord) -> ArchivedResponse:
        return self.read_at(record.offset, record.length)

    def _parse(self, raw: bytes) -> ArchivedResponse:
        head, _, rest = raw.partition(CRLF + CRLF)
        _, warc_pairs = _split_headers(head)
        warc_headers = {n.decode().lower(): v.decode() for n, v in warc_pairs}

        # Trust Content-Length. Scanning for the terminating CRLFs instead
        # would truncate any body that happens to contain a blank line.
        block = rest[:int(warc_headers["content-length"])]
        status_head, _, body = block.partition(CRLF + CRLF)
        status_line, raw_headers = _split_headers(status_head)

        version, _, remainder = status_line.decode("latin-1").partition(" ")
        code, _, reason = remainder.partition(" ")
        headers = {n.decode().lower(): v.decode("latin-1") for n, v in raw_headers}

        declared = int(headers.get("content-length", len(body)))
        if len(body) != declared:
            raise ValueError(f"record body is {len(body)} bytes, "
                             f"Content-Length says {declared}")

        return ArchivedResponse(
            url=warc_headers.get("warc-target-uri", ""), status=int(code),
            reason_phrase=reason, http_version=version, headers=headers,
            raw_headers=raw_headers, body=body, warc_headers=warc_headers)


class ArchiveReplay:
    """A crawl's worth of pages, served from disk instead of the network."""

    def __init__(self, warc_path: str | Path, cdx_path: str | Path):
        self.reader = WarcReader(warc_path)
        self.index = CdxIndex(cdx_path)

    def fetch(self, url: str) -> ArchivedResponse | None:
        """The archive's answer to a URL — the newest capture, or None.

        Note what does NOT happen: no socket, no robots check, no politeness
        wait. Replay is not a second fetch path, and the moment it grows one
        the archive stops being the thing under test.
        """
        captures = self.index.lookup(url)
        return self.reader.read(captures[-1]) if captures else None

    def links(self, url: str) -> list[str] | None:
        """Re-extract outlinks offline, with the same parser the crawl used."""
        response = self.fetch(url)
        if response is None or not response.is_html:
            return None
        # The archive kept the Content-Type, so replay decodes the page
        # exactly as the live crawl did — including its charset.
        return parse(response.body, response.url,
                     response.headers.get("content-type")).links

    def urls(self) -> list[str]:
        return [record.url for record in self.index.prefix("")]
