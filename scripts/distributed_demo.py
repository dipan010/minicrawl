"""Run one crawl across several independent processes.

    uv run python scripts/distributed_demo.py [workers]

The parent starts the test corpus and then gets out of the way. Each child is a
separate OS process with its own event loop, its own HTTP client and no
knowledge of the others — they coordinate only through Redis.

What it checks is not throughput. It is the three guarantees that were free in
one process:

  * every expected page is fetched, by somebody
  * no URL is fetched twice, by anybody
  * the origin is never hit faster than its Crawl-delay, in aggregate
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

import redis as redis_lib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REDIS_URL = "redis://127.0.0.1:6379/0"
PREFIX = "demo"
SEED = "http://127.0.0.1:8081/"
CRAWL_DELAY = 0.2           # what :8081's robots.txt states


def child(index: int, out_path: str) -> None:
    from minicrawl.crawler import CrawlConfig, crawl

    result = asyncio.run(crawl(CrawlConfig(
        seeds=[SEED], max_depth=10, workers=4,
        redis_url=REDIS_URL, redis_prefix=PREFIX)))
    Path(out_path).write_text(json.dumps({
        "index": index,
        "urls": [p.url for p in result.pages],
        "errors": len(result.errors),
        "reclaimed": result.requeued_on_resume,
    }))


def main(n_processes: int = 3) -> int:
    from testsite.server import serve

    redis_lib.Redis.from_url(REDIS_URL, decode_responses=True).flushdb()
    # The corpus, served by the parent only — unless something already is,
    # which is the case when this runs from inside the test suite.
    serve(skip_busy=True)

    tmp = Path(__file__).resolve().parent.parent / ".demo"
    tmp.mkdir(exist_ok=True)
    outputs = [tmp / f"child-{i}.json" for i in range(n_processes)]

    started = time.perf_counter()
    ctx = mp.get_context("spawn")        # no shared memory, no inherited state
    procs = [ctx.Process(target=child, args=(i, str(path)))
             for i, path in enumerate(outputs)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    elapsed = time.perf_counter() - started

    reports = [json.loads(p.read_text()) for p in outputs]
    fetched = [u for r in reports for u in r["urls"]]
    unique = set(fetched)

    manifest = json.loads((Path(__file__).resolve().parent.parent
                           / "testsite" / "manifest.json").read_text())
    expected = set(manifest["expected_pages"])
    covered = {u.split("8081")[1] or "/" for u in unique if "8081" in u}
    missing = sorted(expected - covered)

    print(f"\n{n_processes} processes, {elapsed:.1f}s\n")
    for r in reports:
        print(f"  process {r['index']}: {len(r['urls']):>2} pages fetched, "
              f"{r['errors']} errors, {r['reclaimed']} reclaimed")
    print()
    print(f"  fetched      {len(fetched)} requests, {len(unique)} distinct URLs")
    print(f"  duplicates   {len(fetched) - len(unique)}"
          f"{'  <-- the dedup guarantee held' if len(fetched) == len(unique) else '  <-- BROKEN'}")
    print(f"  coverage     {'complete' if not missing else missing}")

    # Politeness is global or it is nothing: N processes each obeying the delay
    # locally would hit the origin N times faster than it asked for.
    on_host = [u for u in fetched if "127.0.0.1:8081" in u]
    floor = CRAWL_DELAY * (len(on_host) - 1)
    verdict = "held" if elapsed >= floor else "VIOLATED"
    print(f"  politeness   {len(on_host)} requests to :8081 in {elapsed:.1f}s; "
          f"Crawl-delay floor is {floor:.1f}s -> {verdict}")

    for path in outputs:
        path.unlink(missing_ok=True)
    tmp.rmdir()
    return 0 if not missing and len(fetched) == len(unique) and elapsed >= floor else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
