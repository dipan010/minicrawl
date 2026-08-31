"""Stage 10 — the same crawl, written against Scrapy.

The point of this file is not that Scrapy is better or worse. It is that after
nine stages you can read its settings and know exactly which of your own
modules each one replaces, which ones it does not replace, and what it would
cost to close the gap.

The spider below is deliberately *plain*: framework defaults plus the settings
any careful person would set. Everything it does well and everything it misses
is measured by scripts/compare_frameworks.py rather than asserted here.

    uv sync --extra scrapy
    uv run python scrapy_port/run.py http://127.0.0.1:8081/ out.json
"""
from __future__ import annotations

from urllib.parse import urlsplit

import scrapy
from scrapy.linkextractors import LinkExtractor

from minicrawl.traps import TrapGuard


class MinicrawlSpider(scrapy.Spider):
    """A stage-5-equivalent crawl, as far as the framework will take you."""

    name = "minicrawl"

    custom_settings = {
        # -> minicrawl/robots.py + RobotsCache. Scrapy ships a robots.txt
        #    middleware and a parser (Protego), so this whole module is free.
        "ROBOTSTXT_OBEY": True,
        # The product token robots.txt groups on. Scrapy sends this verbatim.
        "USER_AGENT": "minicrawl/0.1 (+https://example.invalid/minicrawl)",
        # -> the one-in-flight-per-host guard in frontier/scheduling.py.
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        # -> minicrawl/politeness.py, roughly. Scrapy does NOT read Crawl-delay,
        #    so this is a hand-set constant where minicrawl reads the site's own
        #    stated value. See the comparison script.
        "DOWNLOAD_DELAY": 0.0,
        # -> CrawlConfig.max_depth / max_pages. Both are framework settings.
        "DEPTH_LIMIT": 10,
        "CLOSESPIDER_PAGECOUNT": 250,
        "RETRY_ENABLED": False,
        "LOG_LEVEL": "ERROR",
        "TELNETCONSOLE_ENABLED": False,
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
    }

    def __init__(self, seed: str, use_trap_guard: str = "0", **kwargs):
        super().__init__(**kwargs)
        # Seeding goes through start_urls, NOT a start_requests() override.
        # Scrapy 2.13 replaced that hook with an async start(), so an override
        # of the old name is now dead code that fails completely silently: no
        # error, no warning, no deprecation notice, zero pages crawled. This
        # cost half an hour, and it is the clearest single argument in the
        # comparison — a framework's extension points are API surface you do
        # not control, and they move.
        self.start_urls = [seed]
        # -> CrawlConfig.same_host, and it does NOT reach. Scrapy scopes by
        #    hostname, never by origin:
        #      get_host_regex() happily builds a regex INCLUDING the port,
        #      should_follow() matches it against urlparse(...).hostname, which
        #      has no port.
        #    So a port-qualified allowed_domains entry compiles to a pattern
        #    that can never match and every request is filtered as offsite —
        #    silently. The crawl fetches the seed, reports success, and stops.
        #    Measured, after assuming the opposite.
        #
        #    Left as the hostname, and the origin check is done by hand below.
        self.origin = urlsplit(seed).netloc
        self.allowed_domains = [urlsplit(seed).hostname]
        self.link_extractor = LinkExtractor()
        # -> minicrawl/traps.py. Scrapy has no trap defence of any kind, so
        #    this is imported rather than reimplemented: the gap is real, and
        #    closing it is a dozen lines once you know what to write.
        self.guard = TrapGuard() if use_trap_guard == "1" else None
        self.seen_paths: list[str] = []

    def parse(self, response):
        yield {"url": response.url, "status": response.status}

        if "text/html" not in response.headers.get("Content-Type", b"").decode("latin-1"):
            return
        for link in self.link_extractor.extract_links(response):
            if urlsplit(link.url).netloc != self.origin:
                continue                      # the scoping allowed_domains cannot do
            if self.guard is not None and self.guard.admit(link.url):
                continue
            yield response.follow(link, callback=self.parse)
