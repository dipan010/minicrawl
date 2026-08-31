"""Stage 3 — robots.txt and per-host politeness.

The parser tests run against the exact robots.txt bodies the corpus serves, so
a change to the corpus cannot silently stop testing the parser.
"""
import pytest

from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.politeness import Politeness
from minicrawl.robots import RobotsTxt, compile_pattern
from testsite import spec

HOST = "127.0.0.1:8081"
AGENT = "minicrawl/0.1 (+https://example.invalid/minicrawl; learning project)"


@pytest.fixture(scope="module")
def primary_robots():
    return RobotsTxt.parse(spec.ROBOTS[8081]["body"])


# --- RFC 9309 status handling --------------------------------------------

@pytest.mark.parametrize("status,expected", [(200, True), (404, True), (410, True)])
def test_missing_robots_means_crawl_freely(status, expected):
    assert RobotsTxt.from_response(status, "").allowed("/anything", AGENT) is expected


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_means_full_disallow(status):
    """§2.3.1.4 — the rule everybody gets backwards. A failing site is not consent."""
    assert RobotsTxt.from_response(status, "").allowed("/anything", AGENT) is False


def test_unreachable_robots_means_full_disallow():
    assert RobotsTxt.from_response(None, "").allowed("/", AGENT) is False


# --- matching semantics ---------------------------------------------------

def test_longest_match_wins_not_first_match(primary_robots):
    """`Allow: /private/public-corner` beats the shorter `Disallow: /private/`."""
    assert primary_robots.allowed("/private/public-corner", AGENT) is True
    assert primary_robots.allowed("/private/secret", AGENT) is False


def test_most_specific_user_agent_group_wins(primary_robots):
    """Our group forbids /files/; the `*` group does not."""
    assert primary_robots.allowed("/files/logo.png", AGENT) is False
    assert primary_robots.allowed("/files/logo.png", "otherbot") is True


def test_wildcard_and_end_anchor(primary_robots):
    """`Disallow: /*.pdf$` in the `*` group hits the PDF and nothing else."""
    assert primary_robots.allowed("/files/report.pdf", "otherbot") is False
    assert primary_robots.allowed("/files/report.pdf.txt", "otherbot") is True


def test_empty_disallow_grants_everything():
    assert RobotsTxt.parse("User-agent: *\nDisallow:\n").allowed("/x", AGENT) is True


def test_tie_between_allow_and_disallow_goes_to_allow():
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /x\nAllow: /x\n")
    assert robots.allowed("/x", AGENT) is True


def test_pattern_compilation():
    assert compile_pattern("/a").match("/abc")          # prefix match by default
    assert not compile_pattern("/a$").match("/abc")     # `$` anchors
    assert compile_pattern("/*.pdf$").match("/x/y.pdf")


def test_crawl_delay_and_sitemap_are_read(primary_robots):
    assert primary_robots.crawl_delay(AGENT) == 0.2
    assert primary_robots.sitemaps == ["http://127.0.0.1:8081/sitemap.xml"]


# --- politeness -----------------------------------------------------------

async def test_delay_is_per_host_not_global():
    politeness = Politeness()
    politeness.set_delay("a:1", 0.15)
    politeness.set_delay("b:2", 0.15)
    assert await politeness.wait("a:1") == 0.0      # first hit, no wait
    assert await politeness.wait("b:2") == 0.0      # different host, still no wait
    assert await politeness.wait("a:1") > 0.0       # same host again, now we wait


def test_hostile_crawl_delay_is_clamped():
    politeness = Politeness(max_delay=5.0)
    assert politeness.set_delay("h:1", 86400.0) == 5.0


# --- end to end -----------------------------------------------------------

async def test_disallowed_pages_are_never_fetched(base):
    """This is the flip of stage 2's `characterises_no_robots_support_yet`."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=40, max_depth=6,
                                     max_delay=0.0))
    paths = result.paths(HOST)
    assert "/private/secret" not in paths
    assert "/files/report.pdf" not in paths
    assert "/private/public-corner" in paths        # the Allow: exception still works
    blocked = {u.split("8081")[1] for u in result.blocked_by_robots}
    assert blocked == {"/private/secret", "/files/report.pdf"}


async def test_robots_404_host_allows_everything():
    result = await crawl(CrawlConfig(seeds=["http://127.0.0.1:8082/"], max_pages=20,
                                     max_depth=3, max_delay=0.0))
    assert result.blocked_by_robots == []
    assert len(result.pages) > 10


async def test_robots_500_host_yields_nothing():
    result = await crawl(CrawlConfig(seeds=["http://127.0.0.1:8083/"], max_pages=20,
                                     max_depth=3, max_delay=0.0))
    assert result.pages == []
    assert result.blocked_by_robots == ["http://127.0.0.1:8083/"]


async def test_generator_trap_is_closed_by_robots_on_8084():
    result = await crawl(CrawlConfig(seeds=["http://127.0.0.1:8084/"], max_pages=25,
                                     max_depth=4, max_delay=0.0))
    assert not any(p.startswith("/gen/") for p in result.paths("127.0.0.1:8084"))


async def test_crawl_delay_actually_slows_the_crawl(base):
    """Crawl-delay is the gap between request *starts*, so the time a page takes
    to fetch counts toward it — total sleep is always a little under
    delay x gaps. The wall clock is what has to clear the bar."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=6, max_depth=2,
                                     workers=1))
    gaps = len(result.pages) - 1
    assert result.duration >= 0.2 * gaps
    assert result.worker_seconds_waiting > 0.9 * 0.2 * gaps


async def test_sitemap_directive_is_collected(base):
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=3, max_depth=1,
                                     max_delay=0.0))
    assert result.sitemaps == [f"{base}/sitemap.xml"]
