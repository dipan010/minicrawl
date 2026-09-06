"""Record real crawls so a static page can replay them.

GitHub Pages serves files, not processes, so a hosted crawl is impossible
there. The honest alternative is not to fake one — it is to RECORD one and
play it back, which is a thing this project already believes in: stage 12
replays an archived crawl instead of refetching it, and the archive is the
evidence.

So each demo site is crawled for real, and every page event is stored with the
number of seconds between the crawl starting and that page arriving. The page
then replays those gaps. What a visitor watches is the true shape of a crawl —
including the waiting, which is most of it, and which a fabricated animation
would get wrong in exactly the way that matters.

    uv run python scripts/record_crawls.py > docs/crawls.json
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicrawl.crawler import CrawlConfig, crawl                  # noqa: E402
from minicrawl.dedup import DuplicateIndex                        # noqa: E402
from minicrawl.traps import TrapGuard                             # noqa: E402
from minicrawl.web.server import page_event, summary_event        # noqa: E402
from testsite import server as testsite_server                    # noqa: E402
from testsite import spec                                         # noqa: E402

# What gets recorded is NOT the same list a hosted instance would crawl live.
# The local corpus can never be offered as a live target — it only exists on
# this machine — but it is by far the best thing to watch, because every
# pathology the crawler defends against is in it at once. A recording can show
# it; a hosted crawler could not.
CORPUS = f"http://{spec.host(spec.PRIMARY)}/"

TO_RECORD = (
    (CORPUS, "The test corpus — traps, redirects, duplicates and robots rules, all at once", 40, 0.2),
    ("https://quotes.toscrape.com/", "Quotes to Scrape — pagination and tag pages that are near-duplicates", 25, 1.0),
    ("https://books.toscrape.com/", "Books to Scrape — a deep catalogue, 1,000 products", 25, 1.0),
    ("https://example.com/", "example.com — one page, the smallest crawl there is", 6, 1.0),
)


async def record(seed: str, label: str, max_pages: int, delay: float) -> dict:
    events: list[dict] = []
    started = time.monotonic()

    def capture(page) -> None:
        # The offset is the whole point: politeness is mostly waiting, and a
        # replay that drops the gaps shows a crawler that does not exist.
        events.append({"at": round(time.monotonic() - started, 3),
                       **page_event(page)})

    config = CrawlConfig(seeds=[seed], max_pages=max_pages, max_depth=3,
                         workers=2, default_delay=delay, respect_robots=True,
                         traps=TrapGuard(), dedup=DuplicateIndex(),
                         on_page=capture)
    result = await crawl(config)
    return {"seed": seed, "label": label, "recorded": time.strftime("%Y-%m-%d"),
            "settings": {"max_pages": max_pages, "max_depth": 3,
                         "workers": 2, "delay": delay},
            "events": events, "summary": summary_event(result)}


async def main() -> int:
    servers = testsite_server.serve(skip_busy=True)
    try:
        crawls = []
        for url, label, pages, delay in TO_RECORD:
            sys.stderr.write(f"recording {url} ...\n")
            crawls.append(await record(url, label, pages, delay))
            sys.stderr.write(f"  {len(crawls[-1]['events'])} pages in "
                             f"{crawls[-1]['summary']['duration']}s\n")
    finally:
        for httpd in servers:
            httpd.shutdown()
    json.dump({"crawls": crawls}, sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
