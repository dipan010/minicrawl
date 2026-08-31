"""Stage 8 — sitemaps, conditional GET, recrawl scheduling, priority ordering."""
import asyncio

import pytest

from minicrawl import sitemap
from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.freshness import FreshnessStore
from minicrawl.frontier import HostedFrontier, MemoryFrontier, PriorityQueue, Request
from minicrawl.politeness import Politeness

HOST = "127.0.0.1:8081"


@pytest.fixture
def store(tmp_path):
    s = FreshnessStore(tmp_path / "fresh.sqlite3")
    yield s
    s.close()


# --- sitemaps -------------------------------------------------------------

def test_index_and_urlset_are_different_documents():
    index = sitemap.parse(b'<?xml version="1.0"?><sitemapindex '
                          b'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                          b"<sitemap><loc>http://h/s1.xml</loc></sitemap></sitemapindex>")
    assert index.is_index and [e.url for e in index.entries] == ["http://h/s1.xml"]

    urls = sitemap.parse(b'<?xml version="1.0"?><urlset '
                         b'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                         b"<url><loc>http://h/a</loc><lastmod>2025-02-12</lastmod></url>"
                         b"</urlset>")
    assert not urls.is_index
    assert urls.entries[0].url == "http://h/a" and urls.entries[0].lastmod == "2025-02-12"


def test_a_broken_sitemap_yields_nothing_rather_than_raising():
    """A sitemap is a hint, not a contract. Plenty are truncated or are an HTML
    error page with an XML content type."""
    assert sitemap.parse(b"<urlset><url><loc>unterminated").entries == []
    assert sitemap.parse(b"<html><body>404 not found</body></html>").entries == []


async def test_the_orphan_is_unreachable_without_sitemaps(base, manifest):
    """The control, and the whole argument: nothing links to /orphan, so no
    depth limit or parsing cleverness reaches it."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0))
    assert manifest["sitemap_only"] == ["/orphan"]
    assert "/orphan" not in result.paths(HOST)
    assert set(manifest["expected_pages"]) <= result.paths(HOST)


async def test_sitemaps_find_what_links_cannot(base, manifest):
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     read_sitemaps=True))
    assert "/orphan" in result.paths(HOST)
    assert set(manifest["expected_pages_with_sitemaps"]) <= result.paths(HOST)


async def test_sitemap_index_is_followed_to_its_leaves(base, manifest):
    """robots.txt advertises one sitemap, which is an *index* of two others."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=0, max_delay=0.0,
                                     read_sitemaps=True))
    seeded = {u.split("8081")[1] for u in result.sitemap_urls}
    assert set(manifest["sitemaps"]["urls"]) <= seeded


# --- conditional GET ------------------------------------------------------

async def test_a_second_crawl_is_answered_304(base, store):
    first = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                    freshness=store))
    assert first.not_modified == []

    second = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     freshness=store))
    assert len(second.not_modified) > 15
    assert second.bytes_downloaded < first.bytes_downloaded
    assert second.bytes_saved_by_304 > 0


async def test_a_cached_response_does_not_blind_the_crawl(base, store, manifest):
    """A 304 carries no body, so it carries no links. A crawler that discovers
    only by parsing goes blind the moment its cache starts working — the first
    version of this stage found 27 pages then 10. The stored outlinks are what
    keep discovery alive."""
    await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                            freshness=store))
    second = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     freshness=store))
    assert set(manifest["expected_pages"]) <= second.paths(HOST)
    assert any(p.from_cache and p.n_links > 0 for p in second.pages)


async def test_the_volatile_page_is_never_cached(base, store, manifest):
    """One page that is always fresh and one that is never fresh is what an
    adaptive interval has to tell apart."""
    volatile = f"{base}{manifest['always_changes'][0]}"
    await crawl(CrawlConfig(seeds=[volatile], max_depth=0, max_delay=0.0, freshness=store))
    second = await crawl(CrawlConfig(seeds=[volatile], max_depth=0, max_delay=0.0,
                                     freshness=store))
    assert second.not_modified == []
    assert store.get(volatile).changes == 1


def test_validators_are_sent_only_once_something_is_known(store):
    assert store.validators("http://h/x") == {}
    store.record("http://h/x", etag='"abc"', last_modified="Wed, 12 Feb 2025 10:00:00 GMT",
                 content_hash="h1")
    assert store.validators("http://h/x") == {
        "If-None-Match": '"abc"',
        "If-Modified-Since": "Wed, 12 Feb 2025 10:00:00 GMT"}


# --- the schedule ---------------------------------------------------------

def test_unchanged_doubles_and_changed_halves(store):
    now = 1000.0
    assert store.record("u", content_hash="a", now=now).interval == 3600.0
    assert store.record("u", content_hash="a", now=now + 1).interval == 7200.0
    assert store.record("u", content_hash="a", now=now + 2).interval == 14400.0
    assert store.record("u", content_hash="b", now=now + 3).interval == 7200.0


def test_the_interval_is_clamped_at_both_ends(tmp_path):
    s = FreshnessStore(tmp_path / "c.sqlite3", min_interval=10, max_interval=100,
                       initial_interval=50)
    for n in range(10):
        record = s.record("u", content_hash="same", now=n)
    assert record.interval == 100                       # never stops looking
    for n in range(10, 30):
        record = s.record("u", content_hash=f"h{n}", now=n)
    assert record.interval == 10                        # never hammers
    s.close()


def test_first_sight_is_not_a_change(store):
    """Nothing to compare against yet. Counting it as a change would halve the
    interval of every page the crawler has never seen."""
    assert store.record("u", content_hash="a").changes == 0


def test_a_304_does_not_lose_what_was_known(store):
    store.record("u", etag='"e"', content_hash="h1", body_bytes=500)
    after = store.record("u", not_modified=True)
    assert after.content_hash == "h1" and after.etag == '"e"'
    assert store.bytes_saved == 500


# --- priority ordering ----------------------------------------------------

async def test_a_heap_and_a_fifo_agree_when_work_arrives_in_priority_order():
    """Not a weakness. On a single-seed breadth-first crawl, priority IS arrival
    order, so the two orderings coincide by construction — which is exactly why
    swapping the data structure is safe."""
    async def drain(factory, items):
        frontier = HostedFrontier(Politeness(), queue_factory=factory)
        for url, priority in items:
            frontier.push_nowait(Request(url, priority=priority))
        out = []
        while (request := await frontier.acquire()) is not None:
            out.append(request.url)
            await frontier.release(request.url)
        return out

    in_order = [("http://h/1", 1.0), ("http://h/2", 2.0), ("http://h/3", 3.0)]
    assert await drain(MemoryFrontier, in_order) == await drain(PriorityQueue, in_order)


async def test_they_diverge_when_priority_arrives_out_of_order():
    """Which is when the heap earns its keep: something important discovered
    after less important work is already queued."""
    async def drain(factory, items):
        frontier = HostedFrontier(Politeness(), queue_factory=factory)
        for url, priority in items:
            frontier.push_nowait(Request(url, priority=priority))
        out = []
        while (request := await frontier.acquire()) is not None:
            out.append(request.url)
            await frontier.release(request.url)
        return out

    jumbled = [("http://h/late", 9.0), ("http://h/urgent", -5.0), ("http://h/mid", 1.0)]
    assert await drain(MemoryFrontier, jumbled) == [
        "http://h/late", "http://h/urgent", "http://h/mid"]
    assert await drain(PriorityQueue, jumbled) == [
        "http://h/urgent", "http://h/mid", "http://h/late"]


def test_a_priority_tie_does_not_try_to_compare_requests():
    """Request is not orderable. Without the sequence counter, two equal
    priorities make heapq fall through to comparing the payload and raise."""
    queue = PriorityQueue()
    queue.push(Request("http://h/a", priority=1.0))
    queue.push(Request("http://h/b", priority=1.0))
    assert [queue.pop().url, queue.pop().url] == ["http://h/a", "http://h/b"]


async def test_recrawl_priorities_share_a_scale_with_link_priorities(base, tmp_path):
    """Regression: recrawl seeds once carried raw Unix timestamps (~1.7e9) while
    discovered links carried small depths, so every newly discovered link
    outranked every overdue page by a factor of a billion — the heap fetched
    depth-1 pages before it had finished the work it was asked to do.

    Priority is 'seconds until due' for both now, so overdue pages are negative
    and discovered links are small positives. Every due page must therefore be
    fetched before any link discovered from one."""
    store = FreshnessStore(tmp_path / "scale.sqlite3", min_interval=0.0,
                           initial_interval=0.0, max_interval=10.0)
    await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=3, max_delay=0.0,
                            freshness=store))

    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=2, max_delay=0.0,
                                     freshness=store, recrawl=True,
                                     priority_frontier=True, workers=1))
    depths = [p.depth for p in result.pages]
    last_seed = max(i for i, d in enumerate(depths) if d == 0)
    assert all(d == 0 for d in depths[:last_seed + 1]), (
        "a discovered link was fetched before the recrawl work was finished")
    store.close()
