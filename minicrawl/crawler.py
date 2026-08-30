"""Stage 2 — the crawl loop.

    seed -> frontier -> fetch -> extract -> push links -> repeat

Everything a crawler does beyond this is refinement. Stage 2 deliberately has
no politeness, no concurrency and no persistence, and the caps below are the
band-aids that stand in for them: `max_pages` exists only because nothing here
can survive /gen/*, and `same_host` exists only because nothing here would ever
stop. Later stages remove the need for both.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import extract, fetch
from .frontier import MemoryFrontier, Request


@dataclass(slots=True)
class CrawlConfig:
    seeds: list[str]
    max_pages: int = 100
    max_depth: int = 5
    same_host: bool = True
    timeout: float = 10.0
    user_agent: str = fetch.DEFAULT_UA
    on_page: object = None              # optional callback(Page) for live output


@dataclass(slots=True)
class Page:
    url: str
    final_url: str
    status: int | None
    depth: int
    title: str
    n_links: int
    elapsed: float
    error: str | None = None


@dataclass(slots=True)
class CrawlResult:
    pages: list[Page] = field(default_factory=list)
    errors: list[Page] = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0
    stopped_because: str = "frontier drained"

    @property
    def duration(self) -> float:
        return self.finished - self.started

    def paths(self, host: str) -> set[str]:
        """Crawled paths on one host — the shape the manifest is diffed against."""
        out = set()
        for page in self.pages:
            parts = urlsplit(page.final_url)
            if parts.netloc == host:
                out.add(parts.path or "/")
        return out


def host_of(url: str) -> str:
    return urlsplit(url).netloc


async def crawl(config: CrawlConfig) -> CrawlResult:
    frontier = MemoryFrontier()
    for seed in config.seeds:
        frontier.push(Request(url=seed, depth=0))
    seed_hosts = {host_of(s) for s in config.seeds}

    result = CrawlResult(started=time.perf_counter())
    client = fetch.make_client(timeout=config.timeout, user_agent=config.user_agent)
    try:
        while (request := frontier.pop()) is not None:
            if len(result.pages) >= config.max_pages:
                result.stopped_because = f"max_pages ({config.max_pages}) reached"
                break

            got = await fetch.fetch(client, request.url)
            page = Page(url=request.url, final_url=got.final_url, status=got.status,
                        depth=request.depth, title="", n_links=0, elapsed=got.elapsed,
                        error=got.error)

            if got.error or not got.ok:
                page.error = page.error or f"http {got.status}"
                result.errors.append(page)
                _emit(config, page)
                continue

            if got.is_html:
                found = extract.parse(got.body, got.final_url)
                page.title, page.n_links = found.title, len(found.links)
                if request.depth < config.max_depth:
                    for link in found.links:
                        if config.same_host and host_of(link) not in seed_hosts:
                            continue
                        frontier.push(Request(link, request.depth + 1, via=request.url))

            result.pages.append(page)
            _emit(config, page)
    finally:
        await client.aclose()

    result.finished = time.perf_counter()
    return result


def _emit(config: CrawlConfig, page: Page) -> None:
    if callable(config.on_page):
        config.on_page(page)
