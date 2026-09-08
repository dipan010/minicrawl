"""Measure reader mode, and compare it against crawling the same pages.

The claim under test is not "this is fast". It is that a READ and a CRAWL have
different costs *by construction*, and that the difference is politeness rather
than optimisation. So the benchmark reports both, over the same URLs.

    uv run python scripts/reader_bench.py            # the local corpus
    uv run python scripts/reader_bench.py --real     # real sites, politely
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicrawl.crawler import CrawlConfig, crawl                 # noqa: E402
from minicrawl.reader import read_many                           # noqa: E402
from testsite import server as testsite_server                   # noqa: E402
from testsite import spec                                        # noqa: E402

CORPUS = [f"http://{spec.host(spec.PRIMARY)}{p}" for p in
          ("/", "/a", "/b", "/c", "/docs/", "/docs/one", "/docs/two",
           "/docs/sub/three", "/etag", "/variants", "/dup/near-1",
           "/dup/near-2", "/encoded/latin1", "/compressed", "/hosts")]

# Pages that exist to be read, across several hosts — which is the shape reader
# mode is for. Fifty URLs on ONE host would be a load test, and the crawler is
# what you should use for that.
REAL = [
    "https://example.com/",
    "https://www.rfc-editor.org/rfc/rfc9309.html",
    "https://quotes.toscrape.com/",
    "https://books.toscrape.com/",
    "https://quotes.toscrape.com/tag/inspirational/",
    "https://www.iana.org/help/example-domains",
    "https://httpbin.org/html",
    "https://quotes.toscrape.com/author/Albert-Einstein/",
]


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    def pick(p):
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[index]
    return {"p50": pick(50), "p95": pick(95), "min": ordered[0] if ordered else 0,
            "max": ordered[-1] if ordered else 0,
            "mean": statistics.fmean(ordered) if ordered else 0}


def show(label: str, seconds: list[float], wall: float, n_ok: int, n: int) -> None:
    p = percentiles(seconds)
    print(f"  {label:<34} {n_ok}/{n} ok   wall {wall:6.2f}s   "
          f"p50 {p['p50']*1000:7.1f}ms   p95 {p['p95']*1000:7.1f}ms   "
          f"max {p['max']*1000:7.1f}ms")


async def bench_read(urls, concurrency, respect_robots, label):
    started = time.perf_counter()
    results = await read_many(urls, concurrency=concurrency,
                              respect_robots=respect_robots)
    wall = time.perf_counter() - started
    ok = [r for r in results if r.ok]
    show(label, [r.elapsed for r in results], wall, len(ok), len(results))
    return results, wall


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true",
                    help="use real sites instead of the local corpus")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    urls = REAL if args.real else CORPUS
    servers = [] if args.real else testsite_server.serve(skip_busy=True)
    try:
        print(f"\nreader mode — {len(urls)} URLs, concurrency {args.concurrency}\n")
        # Cold: every host's robots.txt still has to be fetched.
        await bench_read(urls, args.concurrency, True, "read, robots checked (cold)")
        # Warm: the same work with the robots cost removed, to show its size.
        await bench_read(urls, args.concurrency, False, "read, robots skipped")
        _, serial_wall = await bench_read(urls, 1, False, "read, one at a time")

        print("\nthe same number of pages, crawled\n")
        # The seed has to be a site with enough links to reach the same page
        # count. Seeding from example.com would "crawl" one page and compare it
        # against an eight-page read, which is not a comparison.
        seed = "https://quotes.toscrape.com/" if args.real else urls[0]
        started = time.perf_counter()
        result = await crawl(CrawlConfig(
            seeds=[seed], max_pages=len(urls), max_depth=3,
            workers=args.concurrency,
            default_delay=0.2 if not args.real else 1.0, on_page=None))
        wall = time.perf_counter() - started
        show("crawl (one host, polite)", [p.elapsed for p in result.pages],
             wall, len(result.pages), len(result.pages))
        print(f"\n  the crawl spent {result.worker_seconds_waiting:.1f} worker-seconds waiting;")
        print("  that is politeness, not overhead — the reader never queues per host.\n")
    finally:
        for httpd in servers:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
