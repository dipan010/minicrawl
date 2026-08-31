"""Stage 9 — one crawl, many processes.

The logic tests run against fakeredis so they need no container. The two that
prove things about *separate processes* need a real Redis and skip without one.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import fakeredis
import pytest

from minicrawl.frontier import RedisFrontier, RedisPoliteness, Request

ROOT = Path(__file__).resolve().parent.parent
REDIS_URL = "redis://127.0.0.1:6379/0"


def real_redis_available() -> bool:
    try:
        import redis
        return bool(redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.3).ping())
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not real_redis_available(),
                                 reason="no Redis (docker run -d -p 6379:6379 redis:7-alpine)")


@pytest.fixture
def client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def frontier(client):
    return RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)


async def drain(frontier):
    out = []
    while (request := await frontier.acquire()) is not None:
        out.append(request.url)
        await frontier.release(request.url)
    return out


# --- the guarantees that were free in one process -------------------------

def test_dedup_decision_is_the_atomic_add(frontier):
    """SADD's return value IS the answer. Checking membership and then adding
    would let two workers both conclude they were first."""
    assert frontier.push_nowait(Request("http://h/a")) is True
    assert frontier.push_nowait(Request("http://h/a")) is False
    assert frontier.seen_count == 1


async def test_a_second_worker_cannot_take_a_leased_host(client):
    """One in-flight request per host, enforced in shared state rather than in
    a Python set that only this process can see."""
    a = RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)
    b = RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)
    a.push_nowait(Request("http://h/1"))
    a.push_nowait(Request("http://h/2"))

    first = a._take("h")
    assert first is not None
    assert b._take("h") is None            # same host, leased elsewhere

    a._complete(first.url)
    assert b._take("h") is not None        # released, now available


async def test_work_pushed_by_one_worker_is_taken_by_another(client):
    a = RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)
    b = RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)
    a.push_nowait(Request("http://h/only"))
    request = await b.acquire()
    assert request is not None and request.url == "http://h/only"
    await b.release(request.url)


def test_counters_are_shared_not_local(client):
    a = RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)
    b = RedisFrontier(RedisPoliteness(client, prefix="t"), prefix="t", client=client)
    a.push_nowait(Request("http://h/1"))
    assert b._pending_total() == 1          # b never saw the push


# --- recovery -------------------------------------------------------------

def test_a_dead_worker_s_request_is_reclaimed(frontier, client):
    """A lease has a TTL; the request it covers does not. A `proc` entry with no
    matching `lease` is the only surviving evidence that work was handed out.
    Without the sweep, a crashed worker takes its page with it and the crawl
    reports success while quietly missing it."""
    frontier.push_nowait(Request("http://h/1"))
    taken = frontier._take("h")
    assert taken is not None
    assert frontier._pending_total() == 0 and frontier._active_total() == 1

    client.delete("t:lease:h")              # what an expiring TTL does
    assert frontier.reclaim() == 1
    assert frontier._pending_total() == 1 and frontier._active_total() == 0
    assert frontier._take("h").url == "http://h/1"


def test_reclaim_leaves_live_work_alone(frontier):
    frontier.push_nowait(Request("http://h/1"))
    frontier._take("h")                     # lease still held
    assert frontier.reclaim() == 0


# --- ordering -------------------------------------------------------------

async def test_priority_orders_the_shared_queue(frontier):
    for url, priority in [("http://h/c", 3.0), ("http://h/a", 1.0), ("http://h/b", 2.0)]:
        frontier.push_nowait(Request(url, priority=priority))
    assert await drain(frontier) == ["http://h/a", "http://h/b", "http://h/c"]


async def test_equal_priorities_keep_arrival_order(frontier):
    """A ZSET orders equal scores lexicographically, not by insertion. The
    sequence prefix on the member is what restores FIFO within a priority."""
    for url in ("http://h/zebra", "http://h/apple", "http://h/mango"):
        frontier.push_nowait(Request(url, priority=1.0))
    assert await drain(frontier) == ["http://h/zebra", "http://h/apple", "http://h/mango"]


# --- shared politeness ----------------------------------------------------

def test_the_clock_is_shared_between_workers(client):
    """Two processes each obeying a 1 req/s limit locally hit the origin twice
    a second. The limit belongs to the host, so the clock has to be shared."""
    a = RedisPoliteness(client, prefix="t")
    b = RedisPoliteness(client, prefix="t")
    a.set_delay("h", 5.0)
    assert b.delay_for("h") == 5.0          # b never set it
    assert b.seconds_until_ready("h") == 0.0
    a.mark_used("h")
    assert b.seconds_until_ready("h") > 4.0  # b must wait for a's request


def test_an_unknown_host_is_ready_immediately(client):
    assert RedisPoliteness(client, prefix="t").seconds_until_ready("new") == 0.0


# --- with a real Redis, and real processes --------------------------------

@needs_redis
async def test_two_frontiers_on_a_real_server_share_one_crawl():
    import redis as redis_lib
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    a = RedisFrontier(RedisPoliteness(client, prefix="itest"), prefix="itest",
                      client=client)
    a.reset()
    b = RedisFrontier(RedisPoliteness(client, prefix="itest"), prefix="itest",
                      client=client)
    try:
        for n in range(6):
            a.push_nowait(Request(f"http://h{n}/page"))
        taken = []
        for frontier in (a, b, a, b, a, b):
            request = await frontier.acquire()
            taken.append(request.url)
            await frontier.release(request.url)
        assert len(set(taken)) == 6
    finally:
        a.reset()


@needs_redis
def test_three_processes_split_one_crawl_without_duplicating_it():
    """The whole stage, end to end: three OS processes, no shared memory, and
    the dedup / one-per-host / politeness guarantees still hold."""
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "distributed_demo.py"), "3"],
        capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "duplicates   0" in completed.stdout
    assert "coverage     complete" in completed.stdout
    assert "-> held" in completed.stdout
