"""`minicrawl-read` — fetch URLs and print Markdown.

    minicrawl-read https://example.com/
    minicrawl-read --json url1 url2 url3 > pages.jsonl
    cat urls.txt | minicrawl-read --json --concurrency 20 > pages.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .reader import DEFAULT_CONCURRENCY, DEFAULT_TIMEOUT, read_many


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="minicrawl-read",
        description="Fetch URLs and return their content as Markdown.")
    ap.add_argument("urls", nargs="*", help="URLs; omit to read them from stdin")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object per URL (JSONL) instead of Markdown")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--ignore-robots", action="store_true",
                    help="skip the robots.txt check (you own the consequences)")
    ap.add_argument("--max-bytes", type=int, default=2_000_000)
    args = ap.parse_args(argv)

    urls = args.urls or [line.strip() for line in sys.stdin if line.strip()]
    if not urls:
        ap.error("give at least one URL, or pipe a list on stdin")

    results = asyncio.run(read_many(
        urls, concurrency=args.concurrency, timeout=args.timeout,
        respect_robots=not args.ignore_robots, max_bytes=args.max_bytes))

    for result in results:
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False))
        elif result.error:
            print(f"<!-- {result.url}: {result.error} -->", file=sys.stderr)
        else:
            if len(results) > 1:
                print(f"<!-- {result.final_url} -->")
            print(result.markdown)
            print()

    failed = sum(1 for r in results if r.error)
    if not args.json and failed:
        print(f"{failed} of {len(results)} failed", file=sys.stderr)
    # Exit non-zero only when nothing worked: a partial read is a partial
    # success, and a pipeline should get the pages that did come back.
    return 1 if failed == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
