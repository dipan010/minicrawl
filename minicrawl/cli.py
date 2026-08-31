"""Command line for whichever stage is current.

    uv run minicrawl http://127.0.0.1:8081/ --max-pages 50
    uv run minicrawl http://127.0.0.1:8081/ --verify   # diff against the manifest
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .crawler import CrawlConfig, Page, crawl

MANIFEST = Path(__file__).resolve().parent.parent / "testsite" / "manifest.json"


def print_page(page: Page) -> None:
    mark = "!" if page.error else " "
    status = page.error or str(page.status)
    print(f"{mark} d{page.depth} {status:<8} {page.n_links:>3} links  {page.final_url}")


def verify(result, host: str) -> int:
    manifest = json.loads(MANIFEST.read_text())
    expected = set(manifest["expected_pages"])
    got = result.paths(host)
    missing, extra = sorted(expected - got), sorted(got - expected)
    print(f"\n  expected {len(expected)} pages, crawled {len(got)} on {host}")
    for path in missing:
        print(f"  MISSING  {path}")
    for path in extra:
        print(f"  EXTRA    {path}")
    if not missing and not extra:
        print("  exact match against the manifest")
        return 0
    print(f"\n  {len(missing)} missing, {len(extra)} extra")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="minicrawl")
    ap.add_argument("seeds", nargs="+")
    ap.add_argument("--max-pages", type=int, default=100)
    ap.add_argument("--max-depth", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--all-hosts", action="store_true", help="leave the seed hosts")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="crawl as if robots.txt did not exist (stage 2 behaviour)")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="per-host delay when robots.txt states no Crawl-delay")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent workers, shared across all hosts")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="diff the crawl against testsite/manifest.json")
    args = ap.parse_args(argv)

    config = CrawlConfig(
        seeds=args.seeds, max_pages=args.max_pages, max_depth=args.max_depth,
        timeout=args.timeout, same_host=not args.all_hosts,
        respect_robots=not args.ignore_robots, default_delay=args.delay,
        workers=args.workers,
        on_page=None if args.quiet else print_page,
    )
    result = asyncio.run(crawl(config))
    peak = sorted(result.peak_in_flight_per_host.values(), reverse=True)[:1]
    print(f"\n  {result.workers} workers over {result.hosts_seen} host(s), "
          f"peak {result.peak_in_flight} in flight, "
          f"max {peak[0] if peak else 0} per host")
    print(f"  {len(result.pages)} pages, {len(result.errors)} errors, "
          f"{len(result.blocked_by_robots)} blocked by robots.txt, "
          f"{result.duration:.2f}s, {result.worker_seconds_waiting:.1f} worker-s waiting "
          f"— stopped: {result.stopped_because}")
    if args.verify:
        return verify(result, urlsplit(args.seeds[0]).netloc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
