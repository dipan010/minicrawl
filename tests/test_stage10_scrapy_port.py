"""Stage 10 — the comparison, asserted.

These pin the *measured* differences between minicrawl and Scrapy so that a
version bump in either one shows up as a failing test rather than as a stale
claim in a document.
"""
import importlib.util

import pytest

HAS_SCRAPY = importlib.util.find_spec("scrapy") is not None
needs_scrapy = pytest.mark.skipif(not HAS_SCRAPY,
                                  reason="scrapy not installed (uv sync --extra scrapy)")


@needs_scrapy
def test_scrapy_canonicalisation_keeps_tracking_parameters():
    """w3lib sorts query parameters and drops the fragment, but keeps utm_* and
    session ids. minicrawl.normalize drops them, so the corpus's five queried
    spellings of /a collapse to one there and to three here."""
    from w3lib.url import canonicalize_url

    from minicrawl.normalize import normalize

    spellings = ["http://h/a?b=2&a=1", "http://h/a?a=1&b=2",
                 "http://h/a?a=1&b=2&utm_source=news", "http://h/a?sid=99a1&a=1&b=2"]
    assert len({canonicalize_url(u) for u in spellings}) == 3
    assert len({normalize(u) for u in spellings}) == 1


@needs_scrapy
def test_protego_and_minicrawl_agree_on_the_rules_themselves():
    """The parsers agree. The divergence is in what each does when robots.txt
    cannot be fetched, not in how the rules are read."""
    from protego import Protego

    from minicrawl.robots import RobotsTxt
    from testsite import spec

    body = spec.ROBOTS[8081]["body"]
    theirs, mine = Protego.parse(body), RobotsTxt.parse(body)
    agent = "minicrawl/0.1 (+https://example.invalid/minicrawl)"
    for path in ("/a", "/private/secret", "/private/public-corner", "/files/report.pdf"):
        assert theirs.can_fetch(f"http://127.0.0.1:8081{path}", agent) == \
            mine.allowed(path, agent), path


@needs_scrapy
def test_scrapy_cannot_scope_to_an_origin():
    """get_host_regex() builds a pattern INCLUDING the port; should_follow()
    matches it against a hostname, which has none. A port-qualified
    allowed_domains therefore filters everything, silently — the crawl fetches
    the seed, reports success and stops."""
    import scrapy
    from scrapy.downloadermiddlewares.offsite import OffsiteMiddleware
    from scrapy.utils.test import get_crawler

    class Scoped(scrapy.Spider):
        name = "scoped"
        allowed_domains = ["127.0.0.1:8081"]

    middleware = OffsiteMiddleware.from_crawler(get_crawler(Scoped))
    regex = middleware.get_host_regex(Scoped())
    assert ":8081" in regex.pattern            # the port is in the pattern
    assert not regex.search("127.0.0.1")       # but a hostname is what it is given


def test_minicrawl_scopes_on_the_origin():
    from minicrawl.frontier import host_of
    assert host_of("http://127.0.0.1:8081/x") == "127.0.0.1:8081"
    assert host_of("http://127.0.0.1:8082/x") != host_of("http://127.0.0.1:8081/x")


@needs_scrapy
def test_scrapy_has_no_crawl_delay_support():
    """Scrapy's DOWNLOAD_DELAY is a constant you choose; it never reads the
    Crawl-delay the site states. AutoThrottle adapts to latency, which is a
    different thing: it protects throughput, not the origin's stated wishes."""
    from scrapy.settings import default_settings
    assert default_settings.DOWNLOAD_DELAY == 0
    assert not any("CRAWL_DELAY" in name for name in dir(default_settings))

    from minicrawl.robots import RobotsTxt
    from testsite import spec
    assert RobotsTxt.parse(spec.ROBOTS[8081]["body"]).crawl_delay("minicrawl") == 0.2


@needs_scrapy
def test_scrapy_ships_no_trap_defence():
    """There is no setting for it; DEPTH_LIMIT and CLOSESPIDER_PAGECOUNT bound
    a crawl, they do not recognise a generated family. Importing minicrawl's
    guard into the spider takes the corpus from 223 generated pages to 3."""
    import scrapy.settings.default_settings as defaults
    assert not any(("TRAP" in n or "SHAPE" in n) for n in dir(defaults))

    from minicrawl.traps import TrapGuard
    guard = TrapGuard(shape_budget=5)
    admitted = [guard.admit(f"http://h/gen/{n}") for n in range(10)]
    assert admitted.count(None) == 5
