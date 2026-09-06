"""Stage 14 — running a crawler versus exposing one.

The same code that is a tool on your laptop is a request-forging service when
it has a public URL. These tests pin the three things that have to change, and
one that must not: the CLI keeps its freedom.
"""
import asyncio
import json

import httpx
import pytest

from minicrawl.web import server as web
from minicrawl.web.policy import DEMO_SITES, LOCAL, PUBLIC, Policy, from_env


# --- SSRF -----------------------------------------------------------------

@pytest.mark.parametrize("seed", [
    "http://127.0.0.1:8081/",          # the box itself
    "http://localhost:6379/",          # whatever else it runs
    "http://169.254.169.254/",         # cloud metadata: hands out credentials
    "http://[::1]:8000/",              # loopback, v6
    "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://0.0.0.0/",
])
def test_a_public_deployment_refuses_to_fetch_its_own_network(seed):
    assert PUBLIC.check_seed(seed) is not None, f"{seed} was allowed"


def test_an_open_public_policy_still_blocks_private_addresses():
    """Even without an allowlist — the guard is the address, not the list."""
    open_policy = Policy(allow_private=False, public=True)
    assert open_policy.check_seed("http://169.254.169.254/") is not None
    assert open_policy.check_seed("http://127.0.0.1:8081/") is not None
    assert open_policy.check_seed("https://example.com/") is None


def test_the_name_is_resolved_rather_than_pattern_matched():
    """`localtest.me` is a public hostname that resolves to 127.0.0.1.

    A substring check for "localhost" or a leading "127." waves it straight
    through, which is why the policy resolves the name and inspects every
    address it maps to.
    """
    open_policy = Policy(allow_private=False, public=True)
    assert "localhost" not in "localtest.me"          # the check that would fail
    assert open_policy.check_seed("http://localtest.me/") is not None


def test_a_name_that_does_not_resolve_is_refused_not_attempted():
    open_policy = Policy(allow_private=False, public=True)
    assert open_policy.check_seed("http://no-such-host.invalid/") is not None


def test_the_refusal_does_not_say_which_private_address_exists():
    """A precise answer here is a free port scanner: 'blocked' versus 'no such
    host' would map the private network one guess at a time."""
    open_policy = Policy(allow_private=False, public=True)
    blocked = open_policy.check_seed("http://10.0.0.5/")
    missing = open_policy.check_seed("http://no-such-host.invalid/")
    assert blocked == missing


def test_locally_the_loopback_corpus_is_still_allowed():
    """The whole test corpus lives at 127.0.0.1, and the person typing owns
    the consequences. Hardening the public case must not break the local one."""
    assert LOCAL.check_seed("http://127.0.0.1:8081/") is None


# --- the allowlist --------------------------------------------------------

def test_the_public_demo_accepts_only_its_listed_sites():
    for url, _ in DEMO_SITES:
        assert PUBLIC.check_seed(url) is None, url
    assert PUBLIC.check_seed("https://en.wikipedia.org/") is not None


def test_a_listed_site_matches_with_or_without_its_trailing_slash():
    assert PUBLIC.check_seed(DEMO_SITES[0][0].rstrip("/")) is None


# --- rate and concurrency -------------------------------------------------

def test_a_visitor_cannot_start_crawls_faster_than_the_cooldown():
    policy = Policy(per_ip_cooldown=30.0, public=True)
    assert policy.check_rate("1.2.3.4") is None
    policy.begin("1.2.3.4"); policy.end("1.2.3.4")
    assert policy.check_rate("1.2.3.4") is not None
    assert policy.check_rate("5.6.7.8") is None, "one visitor must not block another"


def test_the_server_refuses_work_beyond_its_concurrency_cap():
    policy = Policy(max_concurrent=2, public=True)
    policy.begin("a"); policy.begin("b")
    assert policy.check_rate("c") is not None
    policy.end("a")
    assert policy.check_rate("c") is None


def test_the_running_count_survives_a_crawl_that_raises():
    """A crawl that dies must not permanently consume a concurrency slot."""
    policy = Policy(max_concurrent=1, public=True)
    try:
        policy.begin("a")
        raise RuntimeError("boom")
    except RuntimeError:
        policy.end("a")
    assert policy.check_rate("b") is None


# --- caps -----------------------------------------------------------------

def test_the_public_caps_are_lower_than_the_local_ones():
    assert PUBLIC.max_pages < LOCAL.max_pages
    assert PUBLIC.min_delay > LOCAL.min_delay


def test_clamping_cannot_be_argued_out_of():
    pages, workers, delay = PUBLIC.clamp(10 ** 6, 512, 0.0)
    assert (pages, workers) == (PUBLIC.max_pages, PUBLIC.max_workers)
    assert delay == PUBLIC.min_delay


# --- defaults -------------------------------------------------------------

def test_a_deployment_that_forgets_the_flag_gets_the_safe_policy(monkeypatch):
    """PUBLIC is opt-in. Nothing about a missing env var should quietly widen
    what a server accepts — but note the direction: without the flag the
    server is LOCAL, which is safe only because it is not exposed."""
    monkeypatch.delenv("MINICRAWL_PUBLIC", raising=False)
    assert from_env() is LOCAL
    monkeypatch.setenv("MINICRAWL_PUBLIC", "1")
    assert from_env() is PUBLIC
    monkeypatch.setenv("MINICRAWL_PUBLIC", "true")
    assert from_env() is LOCAL, "only an exact '1' opts in"


def test_config_tells_the_page_what_this_deployment_allows():
    described = PUBLIC.describe()
    assert described["public"] is True
    assert [s["url"] for s in described["sites"]] == [u for u, _ in DEMO_SITES]
    assert LOCAL.describe()["sites"] == [], "a local run offers free text, not a list"


# --- over the wire --------------------------------------------------------

async def _get(port, path, **params):
    async with httpx.AsyncClient() as client:
        return await client.get(f"http://127.0.0.1:{port}{path}",
                                params=params, timeout=30)


async def _stream(port, **params):
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", f"http://127.0.0.1:{port}/crawl",
                                 params=params, timeout=60) as response:
            return "".join([chunk async for chunk in response.aiter_text()])


@pytest.fixture
async def public_server(monkeypatch):
    monkeypatch.setattr(web, "SERVER_POLICY", PUBLIC)
    port = web.free_port()
    server = await asyncio.start_server(web._handle, "127.0.0.1", port)
    yield port
    server.close()
    await server.wait_closed()


async def test_the_health_check_render_polls_answers(public_server):
    response = await _get(public_server, "/healthz")
    assert response.status_code == 200 and response.text == "ok"


async def test_config_is_served_as_json(public_server):
    body = json.loads((await _get(public_server, "/config")).text)
    assert body["public"] is True and len(body["sites"]) == len(DEMO_SITES)


async def test_a_disallowed_seed_is_refused_before_a_single_request(public_server):
    raw = await _stream(public_server, url="http://169.254.169.254/")
    assert "event: failed" in raw
    assert "event: page" not in raw and "event: started" not in raw


def test_the_forwarded_header_is_used_for_rate_limits_only():
    """X-Forwarded-For is trivially spoofable. Spreading rate limits over it is
    fine — a spoofer only gives themselves a fresh bucket. Deciding ACCESS on
    it would let the same spoof bypass the guard entirely, so nothing does."""
    class FakeWriter:
        def get_extra_info(self, _):
            return ("203.0.113.9", 5000)

    assert web.client_of(FakeWriter(), {"x-forwarded-for": "1.1.1.1, 2.2.2.2"}) == "1.1.1.1"
    assert web.client_of(FakeWriter(), {}) == "203.0.113.9"


# --- the CLI keeps its freedom --------------------------------------------

def test_the_command_line_is_not_narrowed_by_any_of_this():
    """None of these limits belong to the crawler — they belong to exposing it.
    A human running the CLI can still crawl their own machine."""
    from minicrawl.cli import main                    # noqa: F401
    assert LOCAL.check_seed("http://127.0.0.1:8081/") is None
    assert LOCAL.allowlist == ()
    assert LOCAL.per_ip_cooldown == 0.0


def test_importing_minicrawl_does_not_require_the_optional_extras(monkeypatch):
    """The container caught this, and no local test could have.

    `frontier/__init__` imports the Redis frontier, which imported `redis` at
    module scope — so a deployment carrying only the two core dependencies
    could not import the package at all. Every machine here has the
    `distributed` extra installed, so the import always succeeded and the bug
    was invisible until a slim image tried it.

    Asserting it therefore means actually making the module unavailable:
    reloading with `redis` installed proves nothing.
    """
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def no_redis(name, *args, **kwargs):
        if name == "redis" or name.startswith("redis."):
            raise ImportError("No module named 'redis'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_redis)
    for name in [n for n in sys.modules if n.startswith("redis")]:
        monkeypatch.delitem(sys.modules, name)

    module = importlib.reload(importlib.import_module("minicrawl.frontier.redis"))
    assert module.redis_lib is None
    # Importing works; asking for the frontier is what fails, and it says how
    # to fix it rather than dying on a bare ModuleNotFoundError.
    with pytest.raises(RuntimeError, match="distributed"):
        module.RedisFrontier("redis://localhost:6379")

    monkeypatch.undo()
    importlib.reload(importlib.import_module("minicrawl.frontier.redis"))
