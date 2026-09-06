"""Measure a crawl and emit JSON for the dashboard.

Every number the dashboard shows comes from here, and every number here comes
from a crawl that actually ran. The rule is the same one the manifest lives
under: nothing is typed in by hand, so nothing can drift from the truth
without the code that produced it changing too.

    uv run python scripts/dashboard_data.py > docs/dashboard.json
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicrawl.cdx import CdxIndex, surt, write_cdxj          # noqa: E402
from minicrawl.charset import decode                          # noqa: E402
from minicrawl.crawler import CrawlConfig, crawl              # noqa: E402
from minicrawl.dedup import DuplicateIndex                    # noqa: E402
from minicrawl.replay import ArchiveReplay                    # noqa: E402
from minicrawl.traps import TrapGuard                         # noqa: E402
from minicrawl.warc import WarcWriter                         # noqa: E402
from testsite import server as testsite_server                # noqa: E402
from testsite import spec                                     # noqa: E402

BASE = f"http://{spec.host(spec.PRIMARY)}"
MANIFEST = json.loads((ROOT / "testsite" / "manifest.json").read_text())


def page_row(page, base: str) -> dict:
    return {"path": page.final_url.replace(base, "") or "/",
            "url": page.final_url, "status": page.status, "depth": page.depth,
            "title": page.title, "links": page.n_links, "words": page.words,
            "verdict": page.verdict, "duplicate_of": page.duplicate_of,
            "elapsed": round(page.elapsed, 3), "error": page.error}


async def corpus_crawl(tmp: Path) -> dict:
    """The headline run: the full corpus, archived and indexed."""
    warc_path, cdx_path = tmp / "d.warc.gz", tmp / "d.cdxj"
    writer = WarcWriter(warc_path)
    config = CrawlConfig(seeds=[BASE + "/"], max_pages=80, max_depth=5,
                         workers=4, traps=TrapGuard(), dedup=DuplicateIndex(),
                         warc=writer, on_page=None)
    result = await crawl(config)
    lines = write_cdxj(writer.index, cdx_path)

    # Replay with the network unavailable — the stage 12 claim, re-measured.
    replay = ArchiveReplay(warc_path, cdx_path)
    index = CdxIndex(cdx_path)
    replayed_links = sum(len(replay.links(u) or []) for u in replay.urls())

    # Diffed exactly the way `cli.verify` diffs it, so the dashboard cannot
    # quietly grade on an easier curve than the repo's own check: paths with
    # the query stripped, and trap pages counted as BOUNDED rather than
    # unexpected — a general defence can cap a generator, it cannot know to
    # exclude it entirely.
    host = BASE.replace("http://", "")
    found = sorted(result.paths(host))
    expected = MANIFEST["expected_pages"]
    prefixes = tuple(MANIFEST["trap_prefixes"])
    extra = sorted(set(found) - set(expected))
    return {
        "pages": [page_row(p, BASE) for p in result.pages],
        "errors": [page_row(p, BASE) for p in result.errors],
        "duration": round(result.duration, 2),
        "workers": result.workers,
        "peak_in_flight": result.peak_in_flight,
        "peak_per_host": result.peak_in_flight_per_host,
        "dedup": result.dedup_counts,
        "blocked_by_robots": sorted(u.replace(BASE, "") for u
                                    in result.blocked_by_robots),
        "rejected_by_traps": result.rejected_by_traps,
        "bytes_downloaded": result.bytes_downloaded,
        "stopped_because": result.stopped_because,
        "expected": expected,
        "found": found,
        "missing": sorted(set(expected) - set(found)),
        "unexpected": [p for p in extra if not p.startswith(prefixes)],
        "bounded_traps": [p for p in extra if p.startswith(prefixes)],
        "archive": {
            "records": writer.records,
            "bytes": writer.bytes_written,
            "cdx_lines": lines,
            # Every line, not a sample: the dashboard runs the same binary
            # search over them that CdxIndex runs over the file, and it can
            # only do that if it has the file.
            "lines": cdx_path.read_text().splitlines(),
            "replayed_links": replayed_links,
            "replay_bytes_read": replay.reader.bytes_read,
            "archive_bytes": warc_path.stat().st_size,
        },
    }


def binary_search_proof(tmp: Path) -> dict:
    """Build a big index and count how little of it a lookup reads."""
    from minicrawl.cdx import CdxRecord
    n = 20000
    records = [CdxRecord(surt(f"http://example.com/p{i:06d}"), "20260101000000",
                         f"http://example.com/p{i:06d}", "text/html", 200,
                         "sha256:x", i * 100, 100, "a.warc.gz")
               for i in range(n)]
    path = tmp / "big.cdxj"
    write_cdxj(records, path, merge=False)
    index = CdxIndex(path)
    index.lookup("http://example.com/p019999")
    return {"lines": n, "file_bytes": path.stat().st_size,
            "lines_read": index.lines_read}


def charset_findings() -> list[dict]:
    """The three corpus pages, decoded by three different rules."""
    import httpx
    from minicrawl.extract import parse
    out = []
    for path, declared in MANIFEST["non_utf8"].items():
        response = httpx.get(BASE + path)
        header = response.headers.get("content-type")
        good = parse(response.content, BASE + path, header)
        # What every stage before 13 produced from the same bytes.
        naive = parse(response.content.decode("utf-8", "replace").encode(),
                      BASE + path)
        out.append({"path": path, "header": header,
                    "declares": declared["declares"],
                    "really": declared["charset"],
                    "decided_by": good.encoding_source,
                    "before": naive.title, "after": good.title,
                    "links_before": len(naive.links),
                    "links_after": len(good.links)})
    return out


async def real_site(seed: str, max_pages: int, delay: float) -> dict:
    """A site nobody here controls. Polite settings, robots respected."""
    started = time.time()
    config = CrawlConfig(seeds=[seed], max_pages=max_pages, max_depth=3,
                         workers=2, default_delay=delay, respect_robots=True,
                         traps=TrapGuard(), dedup=DuplicateIndex(), on_page=None)
    try:
        result = await asyncio.wait_for(crawl(config), timeout=240)
    except Exception as exc:                                   # noqa: BLE001
        return {"seed": seed, "failed": f"{type(exc).__name__}: {exc}",
                "duration": round(time.time() - started, 1)}
    base = seed.rstrip("/")
    return {"seed": seed, "pages": [page_row(p, base) for p in result.pages],
            "count": len(result.pages), "errors": len(result.errors),
            "duration": round(result.duration, 1),
            "dedup": result.dedup_counts,
            "blocked_by_robots": len(result.blocked_by_robots),
            "bytes_downloaded": result.bytes_downloaded,
            "stopped_because": result.stopped_because}


def robots_delay(host: str) -> dict | None:
    """Read a real robots.txt and report the delay it asks for."""
    import httpx
    from minicrawl.robots import RobotsTxt
    try:
        response = httpx.get(f"https://{host}/robots.txt", timeout=15,
                             follow_redirects=True)
        rules = RobotsTxt.parse(response.text)
        return {"host": host, "status": response.status_code,
                "crawl_delay": rules.crawl_delay("minicrawl")}
    except Exception:                                          # noqa: BLE001
        return None


async def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    servers = testsite_server.serve(skip_busy=True)
    try:
        data = {
            "generated": time.strftime("%Y-%m-%d"),
            "tests": int(subprocess.run(
                ["uv", "run", "pytest", "-q", "--collect-only"],
                capture_output=True, text=True, cwd=ROOT
            ).stdout.strip().splitlines()[-1].split()[0]),
            "corpus": await corpus_crawl(tmp),
            "binary_search": binary_search_proof(tmp),
            "charset": charset_findings(),
            "manifest": {
                "expected_pages": len(MANIFEST["expected_pages"]),
                "normalization_groups": MANIFEST["normalization_groups"],
                "redirect_normalized": MANIFEST["redirect_normalized"],
                "malformed_hrefs": MANIFEST["malformed_hrefs"],
                "dup_pairs": MANIFEST["dup_pairs"],
                "sitemap_only": MANIFEST["sitemap_only"],
                "robots": MANIFEST["robots"],
                "traps": MANIFEST["traps"],
            },
            "real_sites": [],
            "robots_seen": [],
        }
        for seed, pages, delay in [("https://quotes.toscrape.com/", 12, 1.0),
                                   ("https://example.com/", 5, 1.0),
                                   ("https://www.rfc-editor.org/rfc/rfc9309.html", 5, 1.5)]:
            data["real_sites"].append(await real_site(seed, pages, delay))
        for host in ("news.ycombinator.com", "en.wikipedia.org", "www.gov.uk"):
            if (found := robots_delay(host)):
                data["robots_seen"].append(found)
    finally:
        for httpd in servers:
            httpd.shutdown()

    json.dump(data, sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
