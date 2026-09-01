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

from . import extract, fetch, render as render_mod, robots as robots_mod, sitemap as sitemap_mod
from .dedup import DuplicateIndex, Verdict
from .freshness import FreshnessStore
from .frontier import HostedFrontier, PriorityQueue, Request, SqliteFrontier
from .frontier.memory import MemoryFrontier
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
    # -- stage 6 --
    dedup: DuplicateIndex | None = field(default_factory=DuplicateIndex)
    follow_duplicate_links: bool = False
    # -- stage 7 --
    renderer: object | None = None      # a render.Renderer, or None for no browser
    render_everything: bool = False     # control case: skip triage, render all
    # -- stage 8 --
    freshness: FreshnessStore | None = None   # validators + recrawl schedule
    read_sitemaps: bool = False               # a second, independent seed source
    priority_frontier: bool = False           # heap ordering instead of FIFO
    recrawl: bool = False                     # seed from what is due, not from URLs
    # -- stage 9 --
    redis_url: str | None = None              # share the frontier across processes
    redis_prefix: str = "mc"


@dataclass
class Page:
    url: str
    final_url: str
    status: int | None
    depth: int
    title: str
    n_links: int
    elapsed: float
    error: str | None = None
    verdict: str = "new"
    duplicate_of: str | None = None
    words: int = 0
    rendered: bool = False
    render_reasons: list[str] = field(default_factory=list)
    from_cache: bool = False        # answered 304 — no body, no reparse


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
    dedup_counts: dict[str, int] = field(default_factory=dict)
    render_candidates: list[str] = field(default_factory=list)
    rendered_pages: list[str] = field(default_factory=list)
    render_seconds: float = 0.0
    fetch_seconds: float = 0.0
    sitemap_urls: list[str] = field(default_factory=list)
    not_modified: list[str] = field(default_factory=list)
    bytes_downloaded: int = 0
    bytes_saved_by_304: int = 0
    requeued_on_resume: int = 0
    already_done_on_start: int = 0
    # URLs a previous run finished. A resumed crawl fetches only what is left,
    # so coverage has to be judged against this plus what it fetched itself.
    already_done_urls: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.finished - self.started

    def paths(self, host: str, include_previous: bool = False) -> set[str]:
        """Crawled paths on one host — the shape the manifest is diffed against.

        With include_previous, paths a previous run already finished count too.
        A resumed crawl fetches only what is left, so judging it on its own
        pages alone reports every page the first run handled as missing.
        """
        urls = [page.final_url for page in self.pages]
        if include_previous:
            urls += self.already_done_urls
        out = set()
        for url in urls:
            parts = urlsplit(url)
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

    if config.redis_url:
        # The politeness clock has to move too. A per-process clock means two
        # crawlers each politely hit the same origin at the full rate.
        from .frontier import RedisFrontier, RedisPoliteness
        import redis as redis_lib
        client = redis_lib.Redis.from_url(config.redis_url, decode_responses=True)
        politeness = RedisPoliteness(client, prefix=config.redis_prefix,
                                     default_delay=config.default_delay,
                                     min_delay=config.min_delay,
                                     max_delay=config.max_delay)
        frontier = RedisFrontier(politeness, prefix=config.redis_prefix, client=client)
        # Sweep for work abandoned by a process that died holding it.
        result.requeued_on_resume = frontier.reclaim()
    elif config.frontier_path:
        frontier = SqliteFrontier(politeness, config.frontier_path)
        result.requeued_on_resume = frontier.recovered
        result.already_done_urls = frontier.done_urls()
        result.already_done_on_start = len(result.already_done_urls)
    else:
        frontier = HostedFrontier(
            politeness,
            queue_factory=PriorityQueue if config.priority_frontier else MemoryFrontier)

    seeds = [u for u in (canonical(s) for s in config.seeds) if u]
    seed_hosts = {host_of(s) for s in seeds}

    if config.recrawl and config.freshness is not None:
        # Recrawl mode: the URLs are not given, they are *chosen* — everything
        # the schedule says is due, most overdue first. This is the case where
        # frontier ordering finally earns its keep: with a page budget, arrival
        # order fetches whichever URL happened to be enqueued first, while the
        # heap fetches the pages that have gone longest without a look.
        now = time.time()
        for url in config.freshness.due_urls():
            record = config.freshness.get(url)
            if config.same_host and host_of(url) not in seed_hosts:
                continue
            # Seconds until due: negative for overdue, and on the same scale as
            # every other priority in the system.
            overdue = (record.next_due - now) if record else 0.0
            frontier.push_nowait(Request(url=url, depth=0, priority=overdue))
    else:
        for seed in seeds:
            frontier.push_nowait(Request(url=seed, depth=0))
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
            handled = True
            try:
                if budget <= 0:
                    # Never processed. Releasing it as finished would mark it
                    # done in a durable frontier and every resume would skip it.
                    handled = False
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
                validators = (config.freshness.validators(request.url)
                              if config.freshness else {})
                try:
                    got = await fetch.fetch(client, request.url, headers=validators or None)
                    result.fetch_seconds += got.elapsed
                    result.bytes_downloaded += len(got.body)
                finally:
                    in_flight -= 1
                    per_host_in_flight[host] -= 1

                page = Page(url=request.url, final_url=got.final_url, status=got.status,
                            depth=request.depth, title="", n_links=0,
                            elapsed=got.elapsed, error=got.error)

                if got.not_modified:
                    # The cheapest possible answer: unchanged, and no body sent.
                    # Nothing to parse and nothing to dedup — but also no links,
                    # and a crawler that discovers only by parsing goes blind the
                    # moment its cache starts working. The remembered outlinks
                    # are what keep discovery alive across a cached response.
                    page.from_cache = True
                    known = config.freshness.get(request.url) if config.freshness else None
                    if config.freshness:
                        before = config.freshness.bytes_saved
                        config.freshness.record(request.url, not_modified=True)
                        result.bytes_saved_by_304 += (config.freshness.bytes_saved - before)
                    if known and request.depth < config.max_depth:
                        for link in known.links:
                            if config.same_host and host_of(link) not in seed_hosts:
                                continue
                            if frontier.known(link):
                                continue
                            if config.traps and config.traps.admit(link):
                                continue
                            await frontier.push(
                                Request(link, request.depth + 1, via=request.url,
                                        priority=request.depth + 1))
                    page.n_links = len(known.links) if known else 0
                    result.not_modified.append(request.url)
                    result.pages.append(page)
                    _emit(config, page)
                    continue

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

                    # Triage on the response we already have: does a browser
                    # have anything to add? Escalate only if it does.
                    if config.renderer is not None:
                        verdict = render_mod.triage(found, len(got.body))
                        if config.render_everything or verdict.should_render:
                            page.render_reasons = verdict.reasons or ["render_everything"]
                            result.render_candidates.append(got.final_url)
                            started = time.perf_counter()
                            html = await config.renderer.render(got.final_url)
                            result.render_seconds += time.perf_counter() - started
                            if html:
                                # Re-extract from the rendered DOM. Links found
                                # only after JS ran are the entire point.
                                found = extract.parse(html.encode(), got.final_url)
                                page.rendered = True
                                result.rendered_pages.append(got.final_url)

                    page.title, page.n_links = found.title, len(found.links)
                    page.words = len(found.main_text.split())

                    if config.freshness is not None:
                        from .dedup import content_hash
                        config.freshness.record(
                            request.url,
                            etag=got.headers.get("etag"),
                            last_modified=got.headers.get("last-modified"),
                            content_hash=content_hash(found.main_text),
                            body_bytes=len(got.body),
                            links=[u for u in (canonical(l) for l in found.links) if u])

                    duplicate = False
                    if config.dedup is not None:
                        canonical_url = (canonical(found.canonical)
                                         if found.canonical else None)
                        decision = config.dedup.add(got.final_url, found.main_text,
                                                    canonical=canonical_url)
                        page.verdict = decision.verdict.value
                        page.duplicate_of = decision.of
                        # A duplicate's links are duplicates too. Not following
                        # them is what actually kills a generated page family:
                        # the chain dies at the first repeat instead of at a
                        # budget. It also risks coverage, since a page reachable
                        # ONLY through a duplicate is now unreachable.
                        duplicate = (
                            not config.follow_duplicate_links
                            and decision.verdict in (Verdict.EXACT_DUPLICATE,
                                                     Verdict.NEAR_DUPLICATE,
                                                     Verdict.ALREADY_SEEN))

                    if not duplicate and request.depth < config.max_depth:
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
                                Request(link, request.depth + 1, via=request.url,
                                        priority=request.depth + 1))

                result.pages.append(page)
                _emit(config, page)
            finally:
                await frontier.release(request.url, done=handled)

    if config.read_sitemaps:
        # A second, independent source of seeds. Sitemap URLs are pushed at
        # priority -1 so that a priority frontier visits them before anything
        # discovered by following links: the site told us these matter.
        for url in await _sitemap_seeds(client, seeds, robots, canonical):
            if not config.same_host or host_of(url) in seed_hosts:
                # Recorded whether or not the push is new: a sitemap URL that
                # is already queued was still found here, and reporting only
                # the new ones makes sitemaps look like they did less than
                # they did.
                result.sitemap_urls.append(url)
                await frontier.push(Request(url, depth=0, priority=-1.0))

    try:
        await asyncio.gather(*(worker() for _ in range(max(1, config.workers))))
    finally:
        await client.aclose()
        if config.renderer is not None:
            await config.renderer.close()

    if robots is not None:
        result.sitemaps = robots.sitemaps
    if config.traps is not None:
        result.rejected_by_traps = dict(config.traps.rejected)
    if config.dedup is not None:
        result.dedup_counts = dict(config.dedup.counts)
    result.worker_seconds_waiting = (politeness.waited_total
                                     + frontier.worker_seconds_waiting)
    result.hosts_seen = frontier.host_count
    result.finished = time.perf_counter()
    return result


async def _sitemap_seeds(client, seeds, robots, canonical) -> list[str]:
    """Follow sitemap indexes to their leaves and return the page URLs.

    Sitemaps are advertised by robots.txt, so the robots fetch has already
    found them. Where it has not, /sitemap.xml is the conventional guess and
    costs one request per host to rule out.
    """
    pending: list[tuple[str, int]] = []
    if robots is not None and robots.sitemaps:
        pending = [(u, 0) for u in robots.sitemaps]
    else:
        pending = [(f"{urlsplit(s).scheme}://{urlsplit(s).netloc}/sitemap.xml", 0)
                   for s in seeds]

    found: list[str] = []
    visited: set[str] = set()
    while pending:
        url, depth = pending.pop()
        if url in visited or depth > sitemap_mod.MAX_INDEX_DEPTH:
            continue
        visited.add(url)
        got = await fetch.fetch(client, url)
        if not got.ok:
            continue
        parsed = sitemap_mod.parse(got.body)
        for entry in parsed.entries:
            if parsed.is_index:
                pending.append((entry.url, depth + 1))
            elif (normalised := canonical(entry.url)):
                found.append(normalised)
    return found


def _emit(config: CrawlConfig, page: Page) -> None:
    if callable(config.on_page):
        config.on_page(page)
