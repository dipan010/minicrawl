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
    """Stage-2 semantics, pinned. Later stages added robots.txt, concurrency,
    normalisation and trap defences; these tests are about the shape of the
    loop, so they keep running against the loop as stage 2 left it."""
    return await crawl(CrawlConfig(seeds=[base_url], max_pages=60, max_depth=6,
                                   respect_robots=False, workers=1,
                                   normalize_urls=False, traps=None, dedup=None))


async def test_finds_every_expected_page(result, manifest):
    missing = set(manifest["expected_pages"]) - result.paths(HOST)
    assert not missing, f"link extraction missed {sorted(missing)}"


async def test_terminates_only_because_of_the_page_cap(result):
    """Stage 2 has no way to survive /gen/*: max_pages is what stops it, not the
    frontier draining. Stage 5's trap guard is what makes draining possible."""
    assert result.stopped_because.startswith("max_pages")
    assert result.duration < 30


async def test_stays_on_the_seed_host(result):
    other_hosts = {p.final_url for p in result.pages if HOST not in p.final_url}
    assert not other_hosts


async def test_depth_limit_is_enforced(base):
    shallow = await crawl(CrawlConfig(seeds=[f"{base}/"], max_pages=60, max_depth=1,
                                      respect_robots=False, workers=1,
                                      normalize_urls=False, traps=None, dedup=None))
    assert max(p.depth for p in shallow.pages) == 1


async def test_records_errors_without_stopping(result):
    """The redirect loop must land in errors, and the crawl must continue past it."""
    loop_errors = [p for p in result.errors if p.url.endswith("/loop/1")]
    assert loop_errors and loop_errors[0].error == "redirect_loop"
    assert len(result.pages) > 20


# --- known gaps, all now fixed --------------------------------------------
#
# Every characterisation test that lived here has been flipped:
#
#   no_robots_support_yet    -> stage 3, test_disallowed_pages_are_never_fetched
#   no_url_normalisation_yet -> stage 5, test_ten_spellings_of_a_become_two_fetches
#   generator_trap_is_entered-> stage 5, test_generator_trap_is_bounded
#
# Each asserted a shortcoming, and each was written to fail when the stage that
# fixed it landed. Flipping one is what finishing a stage looks like.
