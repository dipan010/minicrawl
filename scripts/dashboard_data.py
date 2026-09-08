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
from minicrawl.index import Index                             # noqa: E402
from minicrawl.reader import read_many                        # noqa: E402
from minicrawl.tokenize import tokens as tokenise             # noqa: E402
from minicrawl.charset import decode                          # noqa: E402
from minicrawl.crawler import CrawlConfig, crawl              # noqa: E402
from minicrawl.dedup import DuplicateIndex                    # noqa: E402
from minicrawl.export import JsonlExporter                    # noqa: E402
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
    exporter = JsonlExporter(tmp / "pages.jsonl")
    config = CrawlConfig(seeds=[BASE + "/"], max_pages=80, max_depth=5,
                         workers=4, traps=TrapGuard(), dedup=DuplicateIndex(),
                         warc=writer, exporter=exporter, on_page=None)
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


# --- stage 16: reader mode -------------------------------------------------

async def reader_numbers(urls: list[str], label: str, seed: str,
                         delay: float) -> dict:
    """Read N URLs, then crawl the same number, and time both."""
    started = time.perf_counter()
    results = await read_many(urls, concurrency=8, respect_robots=True)
    read_wall = time.perf_counter() - started

    started = time.perf_counter()
    crawled = await crawl(CrawlConfig(seeds=[seed], max_pages=len(urls),
                                      max_depth=3, workers=8,
                                      default_delay=delay, on_page=None))
    crawl_wall = time.perf_counter() - started

    latencies = sorted(r.elapsed for r in results)
    return {"label": label, "urls": len(urls),
            "read_wall": round(read_wall, 2),
            "read_p50": round(latencies[len(latencies) // 2] * 1000, 1),
            "read_ok": sum(1 for r in results if r.ok),
            "crawl_wall": round(crawl_wall, 2),
            "crawl_pages": len(crawled.pages),
            "waiting": round(crawled.worker_seconds_waiting, 1)}


# --- stages 17 and 18: the index -------------------------------------------

sys.path.insert(0, str(ROOT / "scripts"))
from search_bench import ForwardScan, make_corpus              # noqa: E402



def synthetic(n: int) -> list[str]:
    """The benchmark's own corpus builder — Zipfian, and the same text the
    repo's `scripts/search_bench.py` measures on, so the page cannot quote a
    different number from a different corpus."""
    return make_corpus(n)


def forward_scan_seconds(corpus: list[str], query: str, repeats: int = 3) -> float:
    """Time the honest baseline: BM25 with no inverted index at all.

    Imported from the benchmark rather than reimplemented. The first version
    here computed IDF via `index.idf`, which reads the inverted index's
    document frequencies — so the "scan" was quietly using the very structure
    it was supposed to be the alternative to, and reported an 8x speedup where
    the real figure is 61x. A baseline that borrows from the thing it measures
    is not a baseline.
    """
    scan = ForwardScan()
    for text in corpus:
        scan.add(text)
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        scan.search(query)
        samples.append(time.perf_counter() - started)
    return sorted(samples)[len(samples) // 2]


def timed_query(index: Index, query: str, repeats: int = 30) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        index.search(query)
        samples.append(time.perf_counter() - started)
    return sorted(samples)[len(samples) // 2]


def search_numbers(sizes=(1_000, 10_000, 50_000)) -> dict:
    rows, big = [], None
    for n in sizes:
        corpus = synthetic(n)
        index = Index()
        started = time.perf_counter()
        for i, text in enumerate(corpus):
            index.add(text, url=f"http://example.test/{i}", title=f"Document {i}")
        build = time.perf_counter() - started

        query = "duplicate detection simhash"
        fast = timed_query(index, query)
        slow = forward_scan_seconds(corpus, query)
        rows.append({"documents": n, "build": round(build, 2),
                     "terms": len(index.postings),
                     "index_us": round(fast * 1e6),
                     "scan_us": round(slow * 1e6),
                     "speedup": round(slow / fast)})
        big = index

    # Selectivity, on the largest index: the caveat that belongs next to the
    # headline rather than underneath it.
    selectivity = []
    for query, note in [("w19999", "a term in a handful of documents"),
                        ("duplicate detection simhash", "three ordinary words"),
                        ("w0", "a term in almost every document")]:
        touched = sum(big.doc_frequency(t) for t in set(tokenise(query)))
        selectivity.append({"query": query, "note": note,
                            "us": round(timed_query(big, query) * 1e6),
                            "postings": touched})

    # What positions cost, measured on a smaller index for speed.
    medium = Index()
    for i, text in enumerate(synthetic(10_000)):
        medium.add(text, url=f"http://example.test/{i}", title=f"Document {i}")
    stats = medium.stats()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        with_positions = Path(tmp) / "p.json"
        medium.save(with_positions)
        counts = Path(tmp) / "c.json"
        counts.write_text(json.dumps({
            "version": 1, "k1": medium.k1, "b": medium.b,
            "docs": medium.docs, "lengths": medium.lengths,
            "postings": {t: [(d, len(w)) for d, w in ps.items()]
                         for t, ps in medium.postings.items()}}), encoding="utf-8")
        cost = {"with_positions": with_positions.stat().st_size,
                "counts_only": counts.stat().st_size,
                "per_posting": round(stats["positions"] / stats["postings"], 2)}
    cost["ratio"] = round(cost["with_positions"] / cost["counts_only"], 2)

    phrase_rows = []
    for query, note in [("crawler simhash", "either word, anywhere"),
                        ('"duplicate detection"', "adjacent, in order"),
                        ('"detection duplicate"', "the same words, reversed")]:
        phrase_rows.append({"query": query, "note": note,
                            "us": round(timed_query(medium, query) * 1e6),
                            "docs": len(medium.search(query, limit=100_000))})

    return {"rows": rows, "selectivity": selectivity, "positions": cost,
            "phrases": phrase_rows}


GAP = "\u0000"


def phrase_playground(jsonl: Path) -> dict:
    """Tokenised documents, so the page can run the real intersection.

    Tokenisation happens HERE, in Python, with the same function the index
    uses — the page only performs the positional intersection. Reimplementing
    the tokeniser in JavaScript would mean the demo could disagree with the
    index it claims to demonstrate.
    """
    docs = []
    with jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            text = record.get("text")
            if not text:
                continue
            # A sentinel between the fields, for the same reason the index
            # inserts FIELD_GAP: without it a phrase could span the title and
            # the body, and the demo would match something the real index
            # does not. GAP is not a term any query can produce, so no phrase
            # can cross it.
            terms = (tokenise(record.get("title", "")) + [GAP]
                     + tokenise(text))[:260]
            if len(terms) < 5:
                continue
            docs.append({"url": record.get("final_url", ""),
                         "title": record.get("title", ""),
                         "terms": terms})
    return {"docs": docs[:26]}


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
        sys.stderr.write("reader benchmark ...\n")
        corpus_urls = [f"{BASE}{p}" for p in
                       ("/", "/a", "/b", "/c", "/docs/", "/docs/one",
                        "/docs/two", "/docs/sub/three", "/etag", "/variants",
                        "/dup/near-1", "/encoded/latin1", "/compressed",
                        "/hosts", "/js-only")]
        data["reader"] = [
            await reader_numbers(corpus_urls, "the local corpus",
                                 BASE + "/", 0.2),
            await reader_numbers(
                ["https://example.com/",
                 "https://www.rfc-editor.org/rfc/rfc9309.html",
                 "https://quotes.toscrape.com/",
                 "https://books.toscrape.com/",
                 "https://quotes.toscrape.com/tag/inspirational/",
                 "https://www.iana.org/help/example-domains",
                 "https://httpbin.org/html",
                 "https://quotes.toscrape.com/author/Albert-Einstein/"],
                "real sites", "https://quotes.toscrape.com/", 1.0),
        ]
        sys.stderr.write("search benchmark ...\n")
        data["search"] = search_numbers()
        data["phrase_demo"] = phrase_playground(tmp / "pages.jsonl")
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
