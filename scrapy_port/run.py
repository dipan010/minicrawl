"""Run the Scrapy port and write the crawled URLs to JSON.

Scrapy owns a Twisted reactor that cannot be restarted in-process, and this
project's own crawler runs on asyncio. Rather than fight that, the comparison
runs each crawler in its own process — which is also the fairest way to time
them.

    uv run python scrapy_port/run.py <seed> <out.json> [--traps]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapy.crawler import CrawlerProcess          # noqa: E402

from scrapy_port.spider import MinicrawlSpider     # noqa: E402
from testsite.server import serve                  # noqa: E402


def main() -> int:
    seed, out = sys.argv[1], sys.argv[2]
    use_traps = "--traps" in sys.argv
    serve(skip_busy=True)

    collected: list[dict] = []

    class Collect:
        def process_item(self, item, spider):
            collected.append(dict(item))
            return item

    process = CrawlerProcess(settings={
        "ITEM_PIPELINES": {Collect: 100},
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
    })
    started = time.perf_counter()
    process.crawl(MinicrawlSpider, seed=seed, use_trap_guard="1" if use_traps else "0")
    process.start()
    elapsed = time.perf_counter() - started

    Path(out).write_text(json.dumps({
        "urls": [item["url"] for item in collected],
        "seconds": elapsed,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
