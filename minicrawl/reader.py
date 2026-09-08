"""Stage 16 — reader mode: one URL in, clean Markdown out.

This is the shape an LLM agent actually calls. It is NOT a crawl, and the
difference is the whole point:

    A CRAWL discovers. It follows links, so it needs a frontier, a seen-set,
    trap budgets and depth limits, and it visits one host many times — which
    is why politeness dominates its clock. A polite crawl is mostly waiting,
    by design.

    A READ is told exactly what to fetch. There is no frontier, no discovery,
    and usually one page per host, so the per-host queue that makes crawling
    slow never engages. Latency is one round trip plus a parse.

Same fetcher, same parser, opposite tuning. Reader mode is fast because it is
doing less, not because anything was optimised — and saying so is the honest
version of "lightning fast".

ROBOTS, AND WHY IT IS STILL HERE

A reader could skip robots.txt. Most commercial ones do, on the argument that
a URL a human pasted is a URL a human could have opened in a browser.

This one checks by default and lets the CLI opt out, matching the rule the
rest of the project follows: the person running a command owns what it does,
a form on a web page does not. The cost is one extra request per host, cached
after that — and the benchmark reports cold and warm separately rather than
quoting whichever number flatters it.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .extract import parse
from .fetch import fetch, make_client
from .markdown import to_markdown
from .robots import RobotsTxt, robots_url

DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 10


@dataclass(slots=True)
class ReadResult:
    url: str
    final_url: str = ""
    status: int | None = None
    title: str = ""
    markdown: str = ""
    words: int = 0
    links: int = 0
    elapsed: float = 0.0
    error: str | None = None
    encoding: str = ""
    fetch_seconds: float = 0.0
    parse_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and self.markdown != ""

    def to_dict(self) -> dict:
        return {"url": self.url, "final_url": self.final_url,
                "status": self.status, "title": self.title,
                "markdown": self.markdown, "words": self.words,
                "links": self.links, "elapsed": round(self.elapsed, 3),
                "error": self.error, "encoding": self.encoding}


class RobotsGate:
    """robots.txt, fetched once per host and remembered.

    A read of fifty URLs across three hosts pays for three robots files, not
    fifty. That is the only reason the check is affordable here at all.
    """

    def __init__(self, client, user_agent: str = "minicrawl", timeout: float = 5.0):
        self.client = client
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotsTxt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.fetches = 0

    async def allows(self, url: str) -> bool:
        target = robots_url(url)
        lock = self._locks.setdefault(target, asyncio.Lock())
        async with lock:
            # Under the lock: fifty concurrent reads of one host must produce
            # one robots.txt request, not fifty racing to fill the same slot.
            if target not in self._cache:
                got = await fetch(self.client, target, max_bytes=512_000)
                self.fetches += 1
                self._cache[target] = RobotsTxt.from_response(
                    got.status, got.body.decode("utf-8", "replace"))
        # `allowed` takes a PATH, and the query is part of it: robots rules
        # like `Disallow: /search?` only match when the query is included.
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return self._cache[target].allowed(path, self.user_agent)


async def read_one(client, url: str, *, robots: RobotsGate | None = None,
                   max_bytes: int = 2_000_000) -> ReadResult:
    """Fetch one URL and return it as Markdown."""
    started = time.perf_counter()
    result = ReadResult(url=url)

    if robots is not None and not await robots.allows(url):
        result.error = "blocked by robots.txt"
        result.elapsed = time.perf_counter() - started
        return result

    got = await fetch(client, url, max_bytes=max_bytes)
    result.fetch_seconds = got.elapsed
    result.final_url = got.final_url or url
    result.status = got.status

    if got.error:
        result.error = got.error
    elif not got.ok:
        result.error = f"http {got.status}"
    elif not got.is_html:
        # Not a failure: a PDF is a real answer, it just is not Markdown.
        result.error = f"not html ({got.content_type})"
    else:
        parse_started = time.perf_counter()
        content_type = got.headers.get("content-type")
        found = parse(got.body, result.final_url, content_type)
        result.title = found.title
        result.links = len(found.links)
        result.encoding = found.encoding
        result.markdown = to_markdown(got.body, result.final_url,
                                      content_type=content_type)
        result.words = len(result.markdown.split())
        result.parse_seconds = time.perf_counter() - parse_started

    result.elapsed = time.perf_counter() - started
    return result


async def read_many(urls: list[str], *, concurrency: int = DEFAULT_CONCURRENCY,
                    timeout: float = DEFAULT_TIMEOUT,
                    respect_robots: bool = True,
                    max_bytes: int = 2_000_000) -> list[ReadResult]:
    """Read many URLs at once, in the order they were given.

    A crawl would refuse to do this — one request per host at a time is the
    rule that makes it polite. Here the URLs are a list somebody handed us,
    typically across many hosts, and the concurrency limit is a semaphore
    rather than a per-host queue. Point this at fifty URLs on ONE host and it
    will behave like a small load test, which is why the crawler exists.
    """
    limit = asyncio.Semaphore(concurrency)
    async with make_client(timeout=timeout) as client:
        gate = RobotsGate(client) if respect_robots else None

        async def guarded(url: str) -> ReadResult:
            async with limit:
                try:
                    return await read_one(client, url, robots=gate,
                                          max_bytes=max_bytes)
                except Exception as exc:                     # noqa: BLE001
                    # One bad URL must not lose the other forty-nine.
                    return ReadResult(url=url, error=f"{type(exc).__name__}: {exc}")

        return await asyncio.gather(*(guarded(u) for u in urls))


async def read(url: str, **kwargs) -> ReadResult:
    return (await read_many([url], **kwargs))[0]
