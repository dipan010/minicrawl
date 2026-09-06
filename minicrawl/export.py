"""Stage 15 — getting the crawl out.

A crawler that keeps everything and hands you nothing is a crawler you cannot
use. Stages 11 and 12 gave the archive fidelity and an index; this gives it an
exit. Three shapes, because three different readers want three different
things:

  JSONL   one object per line: URL, status, verdict, and the CLEAN TEXT. This
          is the format everything downstream eats — an index, an embedding
          pipeline, a language model. It streams, so a crawl of any size costs
          one line of memory.

  HTML    one self-contained file a person can open. No server, no build step,
          and NO NETWORK: a report that fetches a webfont is a report that
          renders wrong on the plane where you actually read it. System fonts
          only, everything inlined.

  ZIP     the two above plus the WARC and its index, so a whole crawl travels
          as one file.

WHY THE TEXT COMES FROM THE CRAWL AND NOT FROM THE ARCHIVE

Re-extracting from the WARC afterwards is possible — `replay.py` does exactly
that — and it would be the tidier story. It is also strictly worse here: it
requires `--warc` to have been on, and it re-parses every document a second
time to recover something the crawler was holding when it decided the page was
worth keeping. The exporter takes it at the moment it exists.
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

MAX_TEXT = 200_000          # characters; a runaway page must not own the file


def page_record(page, extracted=None, *, text: bool = True) -> dict:
    """One page, as a downstream reader wants it.

    Written field by field rather than from `asdict`, for the same reason the
    web UI's event is: adding a field to `Page` must not silently change a
    format other programs are parsing.
    """
    record = {
        "url": page.url,
        "final_url": page.final_url,
        "status": page.status,
        "depth": page.depth,
        "title": page.title,
        "links": page.n_links,
        "words": page.words,
        "verdict": page.verdict,
        "duplicate_of": page.duplicate_of,
        "elapsed": round(page.elapsed, 3),
        "error": page.error,
        "from_cache": page.from_cache,
        "rendered": page.rendered,
    }
    if text:
        # `main_text` is the boilerplate-stripped body from stage 6 — the same
        # text duplicate detection reads. Exporting `text` instead would ship
        # the navigation on every page and poison anything built from it.
        body = getattr(extracted, "main_text", "") if extracted else ""
        record["text"] = body[:MAX_TEXT] if body else None
        record["encoding"] = getattr(extracted, "encoding", None) if extracted else None
    return record


class JsonlExporter:
    """Append page records to a JSONL file as the crawl runs.

    Opened lazily so configuring an exporter costs nothing and an empty crawl
    leaves no empty file — the same rule `WarcWriter` follows.
    """

    def __init__(self, path: str | Path, *, text: bool = True):
        self.path = Path(path)
        self.text = text
        self.records = 0
        self._handle = None

    def _open(self):
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8")
        return self._handle

    def record(self, page, extracted=None) -> None:
        handle = self._open()
        handle.write(json.dumps(page_record(page, extracted, text=self.text),
                                ensure_ascii=False) + "\n")
        self.records += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def crawl_summary(result, seeds: list[str]) -> dict:
    """The numbers, in one place, for the report and the bundle manifest."""
    return {
        "seeds": seeds,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "pages": len(result.pages),
        "errors": len(result.errors),
        "duration": round(result.duration, 2),
        "stopped_because": result.stopped_because,
        "workers": result.workers,
        "hosts_seen": result.hosts_seen,
        "peak_in_flight": result.peak_in_flight,
        "bytes_downloaded": result.bytes_downloaded,
        "dedup": dict(result.dedup_counts),
        "blocked_by_robots": list(result.blocked_by_robots),
        "rejected_by_traps": dict(result.rejected_by_traps),
        "not_modified": len(result.not_modified),
        "stored_objects": result.stored_objects,
        "warc_records": result.warc_records,
    }


TEMPLATE = Path(__file__).resolve().parent / "report.html"


def write_report(result, path: str | Path, seeds: list[str]) -> Path:
    """Render one crawl as a single self-contained HTML file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"summary": crawl_summary(result, seeds),
               "pages": [page_record(p, None, text=False) for p in result.pages],
               "errors": [page_record(p, None, text=False) for p in result.errors]}
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null",
        json.dumps(payload, ensure_ascii=False))
    path.write_text(html, encoding="utf-8")
    return path


def write_bundle(path: str | Path, members: dict[str, Path]) -> Path:
    """One zip holding whichever outputs the crawl actually produced.

    Missing members are skipped rather than erroring: a crawl run without
    --warc should still bundle its report and its text.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, source in members.items():
            if source and Path(source).exists():
                archive.write(source, arcname=name)
    return path
