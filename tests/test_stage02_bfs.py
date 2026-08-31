"""Stage 2 — the BFS crawl loop, and an honest record of what it cannot do yet.

The `characterises_` tests below assert the crawler's *current* shortcomings.
They are meant to fail when the stage that fixes each one lands; flipping them
is how you know the next stage actually did something.
"""
import pytest

from minicrawl.crawler import CrawlConfig, crawl

HOST = "127.0.0.1:8081"


@pytest.fixture(scope="module")
async def result(base_url="http://127.0.0.1:8081/"):
    """Stage-2 semantics, pinned: robots.txt off, one worker. Stages 3 and 4
    added both; these tests are about the loop shape, not politeness or
    concurrency."""
    return await crawl(CrawlConfig(seeds=[base_url], max_pages=60, max_depth=6,
                                   respect_robots=False, workers=1))


async def test_finds_every_expected_page(result, manifest):
    missing = set(manifest["expected_pages"]) - result.paths(HOST)
    assert not missing, f"link extraction missed {sorted(missing)}"


async def test_terminates_despite_the_generator_trap(result):
    assert result.stopped_because.startswith("max_pages")
    assert result.duration < 30


async def test_stays_on_the_seed_host(result):
    other_hosts = {p.final_url for p in result.pages if HOST not in p.final_url}
    assert not other_hosts


async def test_depth_limit_is_enforced(base):
    shallow = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=60, max_depth=1,
                                      respect_robots=False, workers=1))
    assert max(p.depth for p in shallow.pages) == 1


async def test_records_errors_without_stopping(result):
    """The redirect loop must land in errors, and the crawl must continue past it."""
    loop_errors = [p for p in result.errors if p.url.endswith("/loop/1")]
    assert loop_errors and loop_errors[0].error == "redirect_loop"
    assert len(result.pages) > 20


# --- known gaps, fixed by later stages ------------------------------------
#
# FLIPPED at stage 3: `test_characterises_no_robots_support_yet` lived here and
# asserted that /private/secret got fetched. It now lives in
# tests/test_stage03_robots.py as test_disallowed_pages_are_never_fetched,
# asserting the opposite. That is what finishing a stage looks like.

async def test_characterises_no_url_normalisation_yet(result):
    """Stage 5 turns this around: 10 spellings of /a collapse to 2 fetches
    (bare /a, and /a?a=1&b=2 — the query is normalised, not discarded)."""
    fetched_a = [p for p in result.pages if p.final_url.startswith("http://127.0.0.1:8081/a")]
    assert len(fetched_a) > 2


async def test_characterises_generator_trap_is_entered(result):
    """Stage 5 turns this around: a path-depth cap keeps /gen/* out entirely."""
    assert any(path.startswith("/gen/") for path in result.paths(HOST))
