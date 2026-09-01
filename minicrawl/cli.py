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
from .dedup import DuplicateIndex
from .freshness import FreshnessStore
from .render import PlaywrightRenderer
from .traps import TrapGuard

MANIFEST = Path(__file__).resolve().parent.parent / "testsite" / "manifest.json"


def print_page(page: Page) -> None:
    mark = "!" if page.error else " "
    status = page.error or str(page.status)
    print(f"{mark} d{page.depth} {status:<8} {page.n_links:>3} links  {page.final_url}")


def report(result) -> None:
    """What the crawl did. Printed always — these are crawl facts, and burying
    them inside --verify meant stages 6, 7 and 8 reported nothing at all unless
    you happened to be diffing against the manifest."""
    if result.dedup_counts:
        counts = result.dedup_counts
        print(f"  documents   {counts.get('new', 0)} unique, "
              f"{counts.get('exact_duplicate', 0)} exact dup, "
              f"{counts.get('near_duplicate', 0)} near dup, "
              f"{counts.get('canonical_alias', 0)} canonical alias, "
              f"{counts.get('already_seen', 0)} refetched")
    if result.sitemap_urls:
        print(f"  sitemaps    {len(result.sitemap_urls)} URLs seeded from sitemaps")
    if result.not_modified:
        print(f"  conditional {len(result.not_modified)} of {len(result.pages)} answered 304 "
              f"— {result.bytes_downloaded:,} bytes downloaded, "
              f"{result.bytes_saved_by_304:,} not sent")
    if result.render_candidates:
        share = len(result.rendered_pages) / max(1, len(result.pages)) * 100
        print(f"  rendered    {len(result.rendered_pages)} of {len(result.pages)} pages "
              f"({share:.0f}%) in {result.render_seconds:.1f}s "
              f"vs {result.fetch_seconds:.1f}s of fetching")
    if result.rejected_by_traps:
        reasons = ", ".join(f"{n} {why}" for why, n in
                            sorted(result.rejected_by_traps.items()))
        print(f"  refused     {reasons}")


def verify(result, host: str) -> int:
    """Diff against ground truth, and nothing else."""
    manifest = json.loads(MANIFEST.read_text())
    expected = set(manifest["expected_pages"])
    prefixes = tuple(manifest["trap_prefixes"])
    # A resumed crawl only fetches what is left, so its own pages are not the
    # measure of coverage — what the frontier already finished counts too.
    got = result.paths(host, include_previous=True)

    missing = sorted(expected - got)
    extra = sorted(got - expected)
    # Trap pages are expected to be *bounded*, not absent: a general defence can
    # cap a generator, it cannot know to exclude it entirely.
    trapped = [p for p in extra if p.startswith(prefixes)]
    unexpected = [p for p in extra if not p.startswith(prefixes)]

    print(f"\n  expected {len(expected)} pages, covered {len(got)} on {host}")
    for path in missing:
        print(f"  MISSING     {path}")
    for path in unexpected:
        print(f"  UNEXPECTED  {path}")
    if trapped:
        print(f"  bounded     {len(trapped)} trap pages under {'/, '.join(prefixes)}"
              f" — capped, not excluded")

    if not missing and not unexpected:
        print("  exact match against the manifest")
        return 0
    print(f"\n  {len(missing)} missing, {len(unexpected)} unexpected")
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
    ap.add_argument("--frontier", metavar="PATH",
                    help="SQLite frontier file; re-run with the same path to resume")
    ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument("--no-traps", action="store_true")
    ap.add_argument("--no-dedup", action="store_true",
                    help="skip content duplicate detection (stage 6)")
    ap.add_argument("--render", action="store_true",
                    help="escalate JS-dependent pages to a headless browser")
    ap.add_argument("--sitemaps", action="store_true",
                    help="read sitemaps as a second seed source")
    ap.add_argument("--freshness", metavar="PATH",
                    help="SQLite freshness store; enables conditional GET across runs")
    ap.add_argument("--priority", action="store_true",
                    help="order the frontier by priority instead of arrival")
    ap.add_argument("--redis", metavar="URL", nargs="?", const="redis://127.0.0.1:6379/0",
                    help="share the frontier across processes via Redis")
    ap.add_argument("--redis-prefix", default="mc",
                    help="key prefix, so two crawls can share one Redis")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="diff the crawl against testsite/manifest.json")
    args = ap.parse_args(argv)

    config = CrawlConfig(
        seeds=args.seeds, max_pages=args.max_pages, max_depth=args.max_depth,
        timeout=args.timeout, same_host=not args.all_hosts,
        respect_robots=not args.ignore_robots, default_delay=args.delay,
        workers=args.workers, frontier_path=args.frontier,
        normalize_urls=not args.no_normalize,
        traps=None if args.no_traps else TrapGuard(),
        dedup=None if args.no_dedup else DuplicateIndex(),
        renderer=PlaywrightRenderer() if args.render else None,
        read_sitemaps=args.sitemaps, priority_frontier=args.priority,
        redis_url=args.redis, redis_prefix=args.redis_prefix,
        freshness=FreshnessStore(args.freshness) if args.freshness else None,
        on_page=None if args.quiet else print_page,
    )
    result = asyncio.run(crawl(config))
    peak = sorted(result.peak_in_flight_per_host.values(), reverse=True)[:1]
    print(f"\n  {result.workers} workers over {result.hosts_seen} host(s), "
          f"peak {result.peak_in_flight} in flight, "
          f"max {peak[0] if peak else 0} per host")
    if result.requeued_on_resume or result.already_done_on_start:
        print(f"  resumed: {result.already_done_on_start} pages already done, "
              f"{result.requeued_on_resume} requeued from a dead worker")
    if args.redis:
        print(f"  shared frontier at {args.redis} under prefix {args.redis_prefix!r}")
    print(f"  {len(result.pages)} pages, {len(result.errors)} errors, "
          f"{len(result.blocked_by_robots)} blocked by robots.txt, "
          f"{result.duration:.2f}s, {result.worker_seconds_waiting:.1f} worker-s waiting "
          f"— stopped: {result.stopped_because}")
    report(result)
    if args.verify:
        return verify(result, urlsplit(args.seeds[0]).netloc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
