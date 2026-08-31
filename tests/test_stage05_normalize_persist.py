"""Stage 5 — one resource one URL, traps bounded, crawls resumable."""
import sqlite3

import pytest

from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.frontier import SqliteFrontier
from minicrawl.frontier.base import Request
from minicrawl.normalize import normalize, normalize_path, url_shape
from minicrawl.politeness import Politeness
from minicrawl.traps import TrapGuard

HOST = "127.0.0.1:8081"


# --- normalisation --------------------------------------------------------

def test_declared_variant_groups_each_collapse_to_one_url(manifest):
    from urllib.parse import urljoin
    for group in manifest["normalization_groups"]:
        got = {normalize(urljoin(f"http://{HOST}/variants", h)) for h in group["hrefs"]}
        assert got == {group["canonical"]}, group["name"]


def test_the_two_groups_do_not_collapse_into_each_other(manifest):
    canonicals = {g["canonical"] for g in manifest["normalization_groups"]}
    assert len(canonicals) == 2, "dropping the query entirely would be wrong"


@pytest.mark.parametrize("url,expected", [
    ("HTTP://Example.COM/a", "http://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:8080/a", "http://example.com:8080/a"),
    ("http://example.com./a", "http://example.com/a"),
    ("http://example.com/a#frag", "http://example.com/a"),
    ("http://example.com/%7Euser", "http://example.com/~user"),
    ("http://example.com/x//y", "http://example.com/x/y"),
])
def test_safe_normalisations(url, expected):
    assert normalize(url) == expected


def test_trailing_slash_is_preserved():
    """The tempting rule, and the wrong one: /docs/ and /docs are different
    resources and this corpus 404s on the second."""
    assert normalize_path("/docs/") == "/docs/"
    assert normalize_path("/docs") == "/docs"


@pytest.mark.parametrize("url", ["ftp://example.com/a", "mailto:x@y.z",
                                 "http://[::bad::]/x", "http://example.com:99999999/a"])
def test_unusable_urls_return_none(url):
    assert normalize(url) is None


async def test_fourteen_spellings_of_a_become_three_fetches(base):
    """The flip of stage 2's `characterises_no_url_normalisation_yet`, which
    recorded ten fetches of /a.

    Three, not two, and the third is the honest cost of the trailing slash:
    normalisation collapses the eight bare spellings to /a and the five queried
    ones to /a?a=1&b=2, but /a/ is a genuinely different URL until the server
    says otherwise. Learning that costs one request. Two distinct pages come
    back from the three."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/variants"], max_depth=1,
                                     max_delay=0.0))
    fetched = [p for p in result.pages if p.url.startswith(f"{base}/a")]
    assert {p.url for p in fetched} == {f"{base}/a", f"{base}/a/", f"{base}/a?a=1&b=2"}
    assert {p.final_url for p in fetched} == {f"{base}/a", f"{base}/a?a=1&b=2"}


async def test_marking_a_landing_url_blocks_a_later_push():
    """Following /a/ to /a must record /a, so a *later* discovery of it is free.

    This is the frontier mechanism, tested directly: a URL already queued when
    the redirect resolves cannot be un-queued, so the saving is on everything
    discovered afterwards -- which, on a real site, is almost all of it."""
    from minicrawl.frontier import HostedFrontier
    frontier = HostedFrontier(Politeness())
    assert await frontier.mark_seen("http://h/a") is True
    assert frontier.known("http://h/a")
    assert frontier.push_nowait(Request("http://h/a")) is False
    assert await frontier.mark_seen("http://h/a") is False


async def test_no_phantom_urls_are_invented(base):
    """Stage 2 turned the unnormalised /a/ plus the relative link `c` into
    /a/c, a URL that does not exist. Normalisation must stop inventing it."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=6, max_delay=0.0))
    assert "/a/c" not in result.paths(HOST)
    assert not [p for p in result.errors if p.error == "http 404"]


# --- traps ----------------------------------------------------------------

def test_url_shape_groups_numeric_segments():
    assert url_shape("http://h/gen/1") == url_shape("http://h/gen/9999")
    assert url_shape("http://h/gen/1") != url_shape("http://h/other/1")
    # Only whole numeric segments collapse; /dup/exact-1 stays distinct.
    assert url_shape("http://h/dup/exact-1") == "h/dup/exact-1"


def test_shape_budget_bounds_a_generated_family():
    guard = TrapGuard(shape_budget=3)
    admitted = [guard.admit(f"http://h/gen/{n}") for n in range(6)]
    assert admitted[:3] == [None, None, None]
    assert admitted[3:] == ["shape_budget"] * 3
    assert guard.rejected["shape_budget"] == 3


def test_other_guards():
    guard = TrapGuard(max_path_depth=3, max_url_length=40, max_repeated_segment=2,
                      max_query_params=2)
    assert guard.admit("http://h/a/b/c/d/e") == "path_too_deep"
    assert guard.admit("http://h/" + "x" * 60) == "url_too_long"
    assert guard.admit("http://h/a/a/a") == "repeated_segment"
    assert guard.admit("http://h/s?a=1&b=2&c=3") == "too_many_params"
    assert guard.admit("http://h/fine") is None


async def test_generator_trap_is_bounded(base):
    """The flip of stage 2's `characterises_generator_trap_is_entered`."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=6, max_delay=0.0,
                                     traps=TrapGuard(shape_budget=5)))
    gen = [p for p in result.paths(HOST) if p.startswith("/gen/")]
    assert len(gen) == 5
    assert result.rejected_by_traps["shape_budget"] > 0


async def test_the_crawl_now_drains_instead_of_hitting_a_cap(base, manifest):
    """The real headline: max_pages stops being what ends the crawl."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=10_000,
                                     max_depth=10, max_delay=0.0))
    assert result.stopped_because == "frontier drained"
    assert set(manifest["expected_pages"]) <= result.paths(HOST)


# --- persistence ----------------------------------------------------------

async def test_a_crawl_resumes_where_it_stopped(base, tmp_path, manifest):
    db = tmp_path / "frontier.sqlite3"
    first = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=8, max_depth=6,
                                    max_delay=0.0, frontier_path=str(db)))
    assert first.stopped_because.startswith("max_pages")
    assert first.already_done_on_start == 0

    second = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=10_000, max_depth=6,
                                     max_delay=0.0, frontier_path=str(db)))
    assert second.already_done_on_start >= len(first.pages)
    assert second.stopped_because == "frontier drained"

    # Neither run alone saw everything; together they did, and no *URL* was
    # requested twice. (Paths can repeat across runs: a redirect chain in the
    # second run can land on a page the first run fetched directly.)
    combined = first.paths(HOST) | second.paths(HOST)
    assert set(manifest["expected_pages"]) <= combined
    assert not ({p.url for p in first.pages} & {p.url for p in second.pages})


async def test_in_flight_rows_are_requeued_after_a_crash(base, tmp_path):
    """A worker that dies mid-request leaves its row marked in_flight. Without
    recovery, every crash silently drops exactly the hardest requests."""
    db = tmp_path / "crash.sqlite3"
    await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=6, max_depth=3,
                            max_delay=0.0, frontier_path=str(db)))

    conn = sqlite3.connect(db)
    conn.execute("UPDATE frontier SET state='in_flight' WHERE state='queued'")
    stranded = conn.total_changes
    conn.commit()
    conn.close()
    assert stranded > 0

    frontier = SqliteFrontier(Politeness(), db)
    assert frontier.recovered == stranded
    assert len(frontier) == stranded
    frontier.close_db()


async def test_the_table_is_the_seen_set(tmp_path):
    frontier = SqliteFrontier(Politeness(), tmp_path / "dedup.sqlite3")
    assert frontier.push_nowait(Request("http://a.test/1")) is True
    assert frontier.push_nowait(Request("http://a.test/1")) is False
    assert frontier.seen_count == 1

    request = await frontier.acquire()
    await frontier.release(request.url)
    assert frontier.done_count == 1
    # A finished URL is never queued again, even on a fresh push.
    assert frontier.push_nowait(Request("http://a.test/1")) is False
    frontier.close_db()


async def test_both_frontiers_produce_the_same_crawl(base, tmp_path):
    """The scheduling is shared; only the storage differs. Prove it."""
    memory = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=6, max_delay=0.0))
    disk = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=6, max_delay=0.0,
                                   frontier_path=str(tmp_path / "same.sqlite3")))
    assert memory.paths(HOST) == disk.paths(HOST)
