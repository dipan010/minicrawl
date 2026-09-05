"""Stage 12 — finding one record in a hundred gigabytes.

Stage 11 produced a WARC where every record is its own gzip member, and then
gave you no way to find anything in it. Locating a URL meant decompressing the
file from the start, which is fine for 27 records and absurd for the hundred
million a real archive holds.

A CDX index is the answer web archives settled on, and it is a sorted text
file. One line per record:

    <surt key> <timestamp> {"url": ..., "offset": ..., "length": ..., ...}

Two properties do all the work:

  SORTED   so a reader can BINARY SEARCH the file by seeking to a byte
           midpoint, without loading the index or even knowing how big it is.
           An index that must be loaded is a hash table with extra steps.
  OFFSETS  so a hit is a seek and one gzip member, not a scan. This is what
           stage 11's one-member-per-record framing was FOR; the two designs
           only pay off together.

SURT (Sort-friendly URI Reordering Transform) is the sort key.
`http://www.example.com/a` becomes `com,example,www)/a`: reversing the host
labels puts every URL from a domain — and every subdomain under it — into one
contiguous run of the file. A range scan then answers "everything archived
under example.com", which is the query archives actually get asked.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def surt(url: str) -> str:
    """Sort-friendly form of a URL.

    IP literals are deliberately NOT reversed. `127.0.0.1` reversed is
    `1,0,0,127` — which sorts an address next to unrelated addresses that
    happen to share a last octet, and implies a hierarchy that does not exist.
    Real SURT implementations leave them alone, and this corpus is served
    entirely from an IP with a port, so it is the case that actually matters
    here rather than a footnote.

    `www.` is kept. Stripping it is common in archival SURT and is the same
    class of guess this project refused at stage 5: a host that serves
    different content at `www` is unusual, not impossible.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""

    try:
        ipaddress.ip_address(host)
        key_host = host                      # an address is not a hierarchy
    except ValueError:
        key_host = ",".join(reversed(host.split("."))) if host else ""

    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{key_host}{port}){path}{query}"


@dataclass(slots=True)
class CdxRecord:
    key: str                # surt, the sort key
    timestamp: str          # YYYYMMDDhhmmss
    url: str
    mime: str
    status: int | None
    digest: str
    offset: int             # byte offset of the gzip member in the WARC
    length: int             # byte length of that member
    filename: str

    def to_line(self) -> str:
        payload = {"url": self.url, "mime": self.mime, "status": self.status,
                   "digest": self.digest, "offset": self.offset,
                   "length": self.length, "filename": self.filename}
        return f"{self.key} {self.timestamp} {json.dumps(payload, sort_keys=True)}"

    @classmethod
    def from_line(cls, line: str) -> "CdxRecord":
        key, timestamp, payload = line.rstrip("\n").split(" ", 2)
        data = json.loads(payload)
        return cls(key=key, timestamp=timestamp, url=data["url"], mime=data["mime"],
                   status=data["status"], digest=data["digest"],
                   offset=data["offset"], length=data["length"],
                   filename=data["filename"])

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.key, self.timestamp)


def write_cdxj(records, path: str | Path) -> int:
    """Write a sorted CDXJ file. Sorting is not tidiness — it is the index."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda r: r.sort_key)
    with path.open("w", encoding="utf-8") as handle:
        for record in ordered:
            handle.write(record.to_line() + "\n")
    return len(ordered)


class CdxIndex:
    """Binary search over the file, never a load.

    Loading the lines into a list and calling `bisect` would teach nothing a
    dict does not already do. The technique that makes a 40 GB index usable is
    seeking to a byte midpoint, scanning forward to the next newline, and
    recursing on a half — so that is what this does, and `lines_read` is
    exposed so a test can prove it.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        self.lines_read = 0

    def _key_of(self, line: bytes) -> str:
        return line.split(b" ", 1)[0].decode("utf-8", "replace")

    def _seek_lower_bound(self, handle, key: str) -> None:
        """Leave `handle` at the first line whose key is >= `key`.

        The search runs over BYTE POSITIONS, not line numbers — the file never
        says how many lines it has, and counting them would be the scan this
        exists to avoid.

        Define g(p) = the first line beginning strictly after byte p (and line
        zero for p = 0). g moves forward as p grows, and the file is sorted, so
        `key(g(p)) >= key` is monotone in p: false, then true. Binary search
        finds the smallest such p, and g(p) is the answer.

        The subtle part is that `low` must advance to `middle + 1`, never to
        wherever the read left the handle. Moving `low` past the line just
        examined can step over the target when the target is the very next
        line, which loses a scattering of keys while every spot check still
        passes.
        """
        low, high = 0, self.size
        while low < high:
            middle = (low + high) // 2
            handle.seek(middle)
            if middle:
                handle.readline()        # discard the line we landed inside
            line = handle.readline()
            self.lines_read += 1
            if line and self._key_of(line) < key:
                low = middle + 1
            else:
                high = middle            # includes EOF: nothing here is < key
        handle.seek(low)
        if low:
            handle.readline()

    def lookup(self, url: str) -> list[CdxRecord]:
        """Every archived capture of one URL, oldest first."""
        key = surt(url)
        found: list[CdxRecord] = []
        with self.path.open("rb") as handle:
            self._seek_lower_bound(handle, key)
            while (line := handle.readline()):
                self.lines_read += 1
                if self._key_of(line) != key:
                    break
                found.append(CdxRecord.from_line(line.decode()))
        return found

    def prefix(self, key_prefix: str) -> list[CdxRecord]:
        """Every capture whose SURT key starts with this — the range query a
        contiguous sort makes possible, and the reason SURT exists."""
        found: list[CdxRecord] = []
        with self.path.open("rb") as handle:
            self._seek_lower_bound(handle, key_prefix)
            while (line := handle.readline()):
                self.lines_read += 1
                if not self._key_of(line).startswith(key_prefix):
                    break
                found.append(CdxRecord.from_line(line.decode()))
        return found

    def __len__(self) -> int:
        with self.path.open("rb") as handle:
            return sum(1 for _ in handle)
