"""Stage 10 — run both crawlers against the same corpus and diff the results.

Each runs in its own process: Scrapy owns a Twisted reactor that cannot be
restarted, minicrawl runs on asyncio, and separate processes are the fairest
way to time them anyway.

Everything printed here is measured, not recalled.

    uv sync --extra scrapy
    uv run python scripts/compare_frameworks.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEED = "http://127.0.0.1:8081/"
HOST = "127.0.0.1:8081"
CRAWL_DELAY = 0.2                       # what :8081's robots.txt states


def key(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def run_minicrawl() -> dict:
    from minicrawl.crawler import CrawlConfig, crawl

    started = time.perf_counter()
    # Stage-5 equivalent: normalisation and trap defence on, content dedup and
    # rendering off, so the comparison is like for like.
    result = asyncio.run(crawl(CrawlConfig(
        seeds=[SEED], max_depth=10, max_pages=250, workers=8,
        dedup=None, renderer=None)))
    return {
        "requests": [p.url for p in result.pages] + [p.url for p in result.errors],
        "seconds": time.perf_counter() - started,
        "blocked_by_robots": len(result.blocked_by_robots),
    }


def run_scrapy(use_traps: bool) -> dict:
    out = ROOT / (".scrapy-traps.json" if use_traps else ".scrapy.json")
    cmd = [sys.executable, str(ROOT / "scrapy_port" / "run.py"), SEED, str(out)]
    if use_traps:
        cmd.append("--traps")
    subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=300)
    data = json.loads(out.read_text())
    out.unlink(missing_ok=True)
    return {"requests": data["urls"], "seconds": data["seconds"], "blocked_by_robots": None}


def summarise(name: str, run: dict, expected: set[str]) -> dict:
    keys = [key(u) for u in run["requests"] if HOST in u]
    distinct = set(keys)
    return {
        "name": name,
        "requests": len(keys),
        "distinct": len(distinct),
        "gen": len({k for k in distinct if k.startswith("/gen/")}),
        "a_spellings": len({k for k in distinct if k == "/a" or k.startswith("/a?")}),
        "robots_violations": sorted(
            k for k in distinct
            if k.startswith("/private/secret") or k.startswith("/files/")),
        "missing": sorted(expected - {k for k in distinct}),
        "seconds": run["seconds"],
    }


def main() -> int:
    from testsite.server import serve
    serve(skip_busy=True)

    manifest = json.loads((ROOT / "testsite" / "manifest.json").read_text())
    expected = set(manifest["expected_pages"])

    rows = [
        summarise("minicrawl (stage 5)", run_minicrawl(), expected),
        summarise("scrapy (defaults)", run_scrapy(False), expected),
        summarise("scrapy + minicrawl.traps", run_scrapy(True), expected),
    ]

    head = f"{'':26} {'reqs':>5} {'pages':>6} {'/gen':>5} {'/a spellings':>13} {'secs':>6}"
    print("\n" + head)
    print("  " + "-" * (len(head) - 2))
    for row in rows:
        print(f"{row['name']:26} {row['requests']:>5} {row['distinct']:>6} "
              f"{row['gen']:>5} {row['a_spellings']:>13} {row['seconds']:>6.1f}")

    print("\n  coverage against the manifest (%d pages expected)" % len(expected))
    for row in rows:
        print(f"    {row['name']:26} {'complete' if not row['missing'] else row['missing']}")

    print("\n  robots.txt: pages fetched that :8081 disallows for this agent")
    for row in rows:
        print(f"    {row['name']:26} {row['robots_violations'] or 'none'}")

    print(f"\n  Crawl-delay: :8081 asks for {CRAWL_DELAY}s between requests")
    for row in rows:
        row_floor = CRAWL_DELAY * (row["requests"] - 1)
        verdict = "honoured" if row["seconds"] >= row_floor else "NOT honoured"
        print(f"    {row['name']:26} {row['requests']} reqs in {row['seconds']:.1f}s "
              f"(floor {row_floor:.1f}s) -> {verdict}")

    print("\n  robots.txt returning HTTP 500 (:8083) — RFC 9309 2.3.1.4 says"
          " assume complete disallow")
    mini = run_minicrawl_on("http://127.0.0.1:8083/")
    scr = run_scrapy_on("http://127.0.0.1:8083/")
    print(f"    {'minicrawl':26} {mini} pages fetched")
    print(f"    {'scrapy (defaults)':26} {scr} pages fetched")
    return 0


def run_minicrawl_on(seed: str) -> int:
    from minicrawl.crawler import CrawlConfig, crawl
    result = asyncio.run(crawl(CrawlConfig(seeds=[seed], max_depth=3, max_pages=30,
                                           max_delay=0.0, dedup=None)))
    return len(result.pages)


def run_scrapy_on(seed: str) -> int:
    out = ROOT / ".scrapy-5xx.json"
    subprocess.run([sys.executable, str(ROOT / "scrapy_port" / "run.py"), seed, str(out)],
                   cwd=ROOT, capture_output=True, timeout=300)
    data = json.loads(out.read_text())
    out.unlink(missing_ok=True)
    return len(data["urls"])


if __name__ == "__main__":
    raise SystemExit(main())
