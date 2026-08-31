"""The crawl loop.

    seed -> frontier -> fetch -> extract -> push links -> repeat

Stage 2 built that as one sequential while-loop. Stage 3 added robots.txt and a
per-host delay. Stage 4 runs N workers over the same five boxes, which changes
one thing structurally: the frontier now decides *which host* a worker may
serve, because "go fast" and "be polite" are constraints on different axes —
parallel across hosts, strictly serial within one.

`max_pages` and `max_depth` remain band-aids, not features: they exist only
because nothing here yet survives /gen/*. Stage 5 removes the need for them.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import extract, fetch, robots as robots_mod
from .frontier import HostedFrontier, Request, SqliteFrontier
from .normalize import normalize
from .politeness import Politeness
from .traps import TrapGuard


@dataclass
class CrawlConfig:
    seeds: list[str]
    max_pages: int = 100
    max_depth: int = 5
    same_host: bool = True
    timeout: float = 10.0
    user_agent: str = fetch.DEFAULT_UA
    on_page: object = None              # optional callback(Page) for live output
    # -- stage 3 --
    respect_robots: bool = True
    robots_agent: str = "minicrawl"     # the product token robots.txt groups on
    default_delay: float = 0.0          # used when robots.txt states no Crawl-delay
    min_delay: float = 0.0              # floor, even if robots.txt says 0
    max_delay: float = 30.0             # ceiling; a hostile Crawl-delay is not binding
    # -- stage 4 --
    workers: int = 8                    # concurrent workers, shared across all hosts
    # -- stage 5 --
    normalize_urls: bool = True
    traps: TrapGuard | None = field(default_factory=TrapGuard)
    frontier_path: str | None = None    # None = in memory; a path = resumable


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
    blocked_by_robots: list[str] = field(default_factory=list)
    worker_seconds_waiting: float = 0.0   # aggregate; exceeds wall clock by design
    sitemaps: list[str] = field(default_factory=list)
    workers: int = 1
    peak_in_flight: int = 0                          # across all hosts
    peak_in_flight_per_host: dict[str, int] = field(default_factory=dict)
    hosts_seen: int = 0
    rejected_by_traps: dict[str, int] = field(default_factory=dict)
    requeued_on_resume: int = 0
    already_done_on_start: int = 0

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


class RobotsCache:
    """One robots.txt per host, fetched once, remembered for the whole crawl.

    Re-fetching robots.txt per request would itself be impolite, and a crawler
    that forgets the rules between requests will eventually race itself into
    fetching something it was told not to.
    """

    def __init__(self, client, agent: str, politeness: Politeness):
        self._client = client
        self._agent = agent
        self._politeness = politeness
        self._cache: dict[str, robots_mod.RobotsTxt] = {}
        self.sitemaps: list[str] = []

    async def get(self, url: str) -> robots_mod.RobotsTxt:
        host = host_of(url)
        if host not in self._cache:
            # The robots.txt request is itself exempt from Crawl-delay: we
            # cannot know the delay until we have read the file that states it.
            got = await fetch.fetch(self._client, robots_mod.robots_url(url))
            parsed = robots_mod.RobotsTxt.from_response(
                got.status, got.body.decode("utf-8", "replace"))
            self._cache[host] = parsed
            self._politeness.set_delay(host, parsed.crawl_delay(self._agent))
            self.sitemaps.extend(parsed.sitemaps)
        return self._cache[host]

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return (await self.get(url)).allowed(path, self._agent)


async def crawl(config: CrawlConfig) -> CrawlResult:
    politeness = Politeness(default_delay=config.default_delay,
                            min_delay=config.min_delay, max_delay=config.max_delay)

    def canonical(url: str) -> str | None:
        return normalize(url) if config.normalize_urls else url

    result = CrawlResult(started=time.perf_counter(), workers=config.workers)

    if config.frontier_path:
        frontier = SqliteFrontier(politeness, config.frontier_path)
        result.requeued_on_resume = frontier.recovered
        result.already_done_on_start = frontier.done_count
    else:
        frontier = HostedFrontier(politeness)

    seeds = [u for u in (canonical(s) for s in config.seeds) if u]
    for seed in seeds:
        frontier.push_nowait(Request(url=seed, depth=0))
    seed_hosts = {host_of(s) for s in seeds}
    client = fetch.make_client(timeout=config.timeout, user_agent=config.user_agent)
    robots = RobotsCache(client, config.robots_agent, politeness) \
        if config.respect_robots else None

    # asyncio is cooperative: nothing preempts between awaits, so a plain int is
    # a sound budget here. It has to be reserved *before* the fetch, or eight
    # workers all pass the check and the crawl overshoots by eight pages.
    budget = config.max_pages
    in_flight = 0
    per_host_in_flight: dict[str, int] = {}

    async def worker() -> None:
        nonlocal budget, in_flight
        while (request := await frontier.acquire()) is not None:
            host = host_of(request.url)
            try:
                if budget <= 0:
                    result.stopped_because = f"max_pages ({config.max_pages}) reached"
                    await frontier.close()
                    return

                if robots is not None and not await robots.allowed(request.url):
                    result.blocked_by_robots.append(request.url)
                    continue

                budget -= 1
                in_flight += 1
                per_host_in_flight[host] = per_host_in_flight.get(host, 0) + 1
                result.peak_in_flight = max(result.peak_in_flight, in_flight)
                result.peak_in_flight_per_host[host] = max(
                    result.peak_in_flight_per_host.get(host, 0), per_host_in_flight[host])
                try:
                    got = await fetch.fetch(client, request.url)
                finally:
                    in_flight -= 1
                    per_host_in_flight[host] -= 1

                page = Page(url=request.url, final_url=got.final_url, status=got.status,
                            depth=request.depth, title="", n_links=0,
                            elapsed=got.elapsed, error=got.error)

                # We followed redirects to get here, so we already hold the
                # content of `final_url`. Record it as seen or it gets queued
                # again under its own spelling and fetched a second time.
                landed = canonical(got.final_url)
                if landed and landed != request.url:
                    await frontier.mark_seen(landed)

                if got.error or not got.ok:
                    page.error = page.error or f"http {got.status}"
                    result.errors.append(page)
                    _emit(config, page)
                    continue

                if got.is_html:
                    found = extract.parse(got.body, got.final_url)
                    page.title, page.n_links = found.title, len(found.links)
                    if request.depth < config.max_depth:
                        for raw in found.links:
                            link = canonical(raw)
                            if link is None:
                                continue
                            if config.same_host and host_of(link) not in seed_hosts:
                                continue
                            # Ask the frontier first: a URL it already knows will
                            # be deduplicated anyway, and must not spend a trap
                            # budget that a genuinely new URL might need.
                            if frontier.known(link):
                                continue
                            if config.traps and config.traps.admit(link):
                                continue
                            await frontier.push(
                                Request(link, request.depth + 1, via=request.url))

                result.pages.append(page)
                _emit(config, page)
            finally:
                await frontier.release(request.url)

    try:
        await asyncio.gather(*(worker() for _ in range(max(1, config.workers))))
    finally:
        await client.aclose()

    if robots is not None:
        result.sitemaps = robots.sitemaps
    if config.traps is not None:
        result.rejected_by_traps = dict(config.traps.rejected)
    result.worker_seconds_waiting = (politeness.waited_total
                                     + frontier.worker_seconds_waiting)
    result.hosts_seen = frontier.host_count
    result.finished = time.perf_counter()
    return result


def _emit(config: CrawlConfig, page: Page) -> None:
    if callable(config.on_page):
        config.on_page(page)
