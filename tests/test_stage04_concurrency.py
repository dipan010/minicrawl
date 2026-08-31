"""Stage 4 — many workers, one clock per host.

The invariant under test is not "it got faster". It is: **concurrency must not
be able to buy its way out of politeness.** A crawler that speeds up by hitting
one host harder has not solved the problem, it has hidden it.
"""
import asyncio
import time

import pytest

from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.frontier import HostedFrontier, Request
from minicrawl.politeness import Politeness

H1, H2, H4 = "http://127.0.0.1:8081", "http://127.0.0.1:8082", "http://127.0.0.1:8084"
THREE_HOSTS = [f"{h}/" for h in (H1, H2, H4)]


# --- the invariant --------------------------------------------------------

async def test_never_more_than_one_request_in_flight_per_host():
    result = await crawl(CrawlConfig(seeds=THREE_HOSTS, max_pages=40, max_depth=4,
                                     workers=16, default_delay=0.05))
    assert result.peak_in_flight_per_host
    assert set(result.peak_in_flight_per_host.values()) == {1}


async def test_peak_concurrency_cannot_exceed_the_host_count():
    """16 workers over 3 hosts still means at most 3 requests in the air."""
    result = await crawl(CrawlConfig(seeds=THREE_HOSTS, max_pages=40, max_depth=4,
                                     workers=16, default_delay=0.05))
    assert result.peak_in_flight <= result.hosts_seen == 3


async def test_workers_do_not_speed_up_a_single_host():
    """Eight workers on one host is still one request per Crawl-delay."""
    result = await crawl(CrawlConfig(seeds=[f"{H1}/"], max_pages=8, max_depth=3,
                                     workers=8))
    assert result.peak_in_flight == 1
    assert result.duration >= 0.2 * (len(result.pages) - 1)


async def test_workers_do_help_across_hosts():
    result = await crawl(CrawlConfig(seeds=THREE_HOSTS, max_pages=40, max_depth=4,
                                     workers=8, default_delay=0.05))
    assert result.peak_in_flight == 3


# --- a slow host must not stall the crawl ---------------------------------

async def test_one_hanging_host_does_not_block_the_others():
    """The strongest proof that the workers are genuinely independent."""
    started = time.perf_counter()
    result = await crawl(CrawlConfig(seeds=[f"{H1}/hang", f"{H2}/"], max_pages=15,
                                     max_depth=3, workers=4, timeout=1.5,
                                     default_delay=0.0, max_delay=0.0))
    elapsed = time.perf_counter() - started
    assert any(p.error == "timeout" for p in result.errors)
    assert len(result.paths("127.0.0.1:8082")) > 5      # the other host got crawled
    assert elapsed < 5.0                                # and did not wait 30s to do it


# --- budgets hold under concurrency ---------------------------------------

@pytest.mark.parametrize("workers", [1, 4, 16])
async def test_max_pages_is_not_overshot_by_the_worker_count(workers):
    """Reserve the budget before the fetch, or N workers all pass the check."""
    result = await crawl(CrawlConfig(seeds=THREE_HOSTS, max_pages=10, max_depth=4,
                                     workers=workers, default_delay=0.0, max_delay=0.0))
    assert len(result.pages) <= 10


async def test_page_set_is_stable_across_worker_counts():
    """Order varies with concurrency; the set of pages found must not."""
    runs = [await crawl(CrawlConfig(seeds=[f"{H2}/"], max_pages=60, max_depth=3,
                                    workers=w, default_delay=0.0, max_delay=0.0))
            for w in (1, 8)]
    assert runs[0].paths("127.0.0.1:8082") == runs[1].paths("127.0.0.1:8082")


# --- the frontier itself --------------------------------------------------

async def test_frontier_drains_to_none():
    frontier = HostedFrontier(Politeness())
    frontier.push_nowait(Request("http://a.test/1"))
    request = await frontier.acquire()
    assert request is not None
    await frontier.release(request.url)
    assert await frontier.acquire() is None


async def test_frontier_holds_a_host_until_released():
    """Two URLs on one host: the second is unreachable while the first is out."""
    frontier = HostedFrontier(Politeness())
    frontier.push_nowait(Request("http://a.test/1"))
    frontier.push_nowait(Request("http://a.test/2"))
    first = await frontier.acquire()

    second = asyncio.create_task(frontier.acquire())
    await asyncio.sleep(0.05)
    assert not second.done()            # blocked: that host is in flight

    await frontier.release(first.url)
    assert (await asyncio.wait_for(second, 1.0)) is not None


async def test_frontier_serves_a_second_host_immediately():
    frontier = HostedFrontier(Politeness())
    frontier.push_nowait(Request("http://a.test/1"))
    frontier.push_nowait(Request("http://b.test/1"))
    first = await frontier.acquire()
    second = await asyncio.wait_for(frontier.acquire(), 1.0)
    assert {first.url, second.url} == {"http://a.test/1", "http://b.test/1"}


async def test_frontier_dedup_is_global_across_host_queues():
    frontier = HostedFrontier(Politeness())
    assert frontier.push_nowait(Request("http://a.test/1")) is True
    assert frontier.push_nowait(Request("http://a.test/1")) is False
    assert frontier.seen_count == 1


async def test_a_push_wakes_a_waiting_worker():
    frontier = HostedFrontier(Politeness())
    frontier.push_nowait(Request("http://a.test/1"))
    held = await frontier.acquire()             # keeps _active at 1, so not drained

    waiter = asyncio.create_task(frontier.acquire())
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await frontier.push(Request("http://b.test/1"))
    assert (await asyncio.wait_for(waiter, 1.0)).url == "http://b.test/1"
    await frontier.release(held.url)
