"""Stage 7 — escalating to a browser, and only where it pays.

Most of these need no browser: the interesting half of this stage is the
triage, which is pure logic over markup already parsed. The handful that do
need Chromium are skipped when it is not installed.
"""
import pytest

from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.extract import parse
from minicrawl.render import (FEW_WORDS, PlaywrightRenderer, playwright_available,
                              triage)
from testsite.server import render_page

HOST = "127.0.0.1:8081"
needs_browser = pytest.mark.skipif(not playwright_available(),
                                   reason="chromium not installed (uv run playwright install chromium)")


def triage_path(path: str):
    html = render_page(path, 8081).encode()
    return triage(parse(html, f"http://{HOST}{path}"), len(html))


# --- the signals are read before boilerplate removal ----------------------

def test_script_bytes_survive_boilerplate_removal():
    """main_text() strips <script>. The signal about scripts has to be taken
    before that happens, or triage can never see it."""
    found = parse(render_page("/js-only", 8081).encode(), f"http://{HOST}/js-only")
    assert found.script_bytes > 0
    assert "getElementById" not in found.main_text


def test_empty_app_root_is_detected():
    found = parse(render_page("/js-only", 8081).encode(), f"http://{HOST}/js-only")
    assert found.empty_app_root is True


def test_a_filled_container_is_not_an_app_root():
    html = b'<html><body><div id="app"><p>real content</p><a href="/x">x</a></div></body></html>'
    assert parse(html, "http://h/").empty_app_root is False


# --- triage ---------------------------------------------------------------

def test_only_the_js_page_is_escalated():
    """The whole stage in one assertion: one page of the corpus needs a browser."""
    escalated = [p for p in ("/", "/a", "/b", "/c", "/docs/", "/docs/one", "/variants",
                             "/dup/near-1", "/hosts", "/slow", "/etag", "/js-only")
                 if triage_path(p)]
    assert escalated == ["/js-only"]


def test_short_pages_without_scripts_are_not_escalated():
    """/docs/one is three words. Short is not the same as unrendered, and a
    rule that fires on length alone escalates most of a documentation site."""
    verdict = triage_path("/docs/one")
    assert not verdict.should_render


def test_reasons_are_reported_not_just_a_score():
    """When a crawl escalates the wrong page you need to know which rule fired."""
    verdict = triage_path("/js-only")
    assert set(verdict.reasons) == {"empty_app_root", "thin_and_script_heavy",
                                    "no_links_with_scripts"}


@pytest.mark.parametrize("html,expected_reason", [
    (b'<html><body><div id="root"></div><script>x</script></body></html>',
     "empty_app_root"),
    (b'<html><body><noscript>Please enable JavaScript</noscript>'
     b'<p>' + b'word ' * 40 + b'</p><a href="/a">a</a></body></html>',
     "noscript_hint"),
])
def test_each_rule_fires_on_its_own_case(html, expected_reason):
    assert expected_reason in triage(parse(html, "http://h/"), len(html)).reasons


def test_script_heavy_needs_thin_text_too():
    """A script-heavy page that is fully rendered server-side must not escalate."""
    prose = ("word " * (FEW_WORDS * 4)).encode()
    html = (b'<html><body><p>' + prose + b'</p><a href="/a">a</a>'
            b'<script>' + b'x' * 5000 + b'</script></body></html>')
    assert not triage(parse(html, "http://h/"), len(html)).should_render


# --- escalation in the crawl loop, with a stub renderer -------------------

class StubRenderer:
    """Records what it was asked to render and returns fixed HTML."""

    def __init__(self, html: str | None = None):
        self.html = html
        self.asked: list[str] = []
        self.closed = False

    async def render(self, url: str) -> str | None:
        self.asked.append(url)
        return self.html

    async def close(self) -> None:
        self.closed = True


async def test_only_triaged_pages_reach_the_renderer(base):
    renderer = StubRenderer('<html><body><h1>x</h1><a href="/js-only/child">c</a></body></html>')
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     renderer=renderer))
    assert [u.split("8081")[1] for u in renderer.asked] == ["/js-only"]
    assert renderer.closed is True
    assert "/js-only/child" in result.paths(HOST)


async def test_a_failing_renderer_does_not_end_the_crawl(base, manifest):
    """A browser that dies is an inconvenience, not a crawl failure: the
    unrendered response is still the result we would otherwise have had."""
    renderer = StubRenderer(None)
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     renderer=renderer))
    assert renderer.asked                       # it was tried
    assert not result.rendered_pages            # and it failed
    assert set(manifest["expected_pages"]) <= result.paths(HOST)


async def test_render_everything_bypasses_triage(base):
    renderer = StubRenderer("<html><body><p>rendered</p></body></html>")
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     renderer=renderer, render_everything=True))
    assert len(renderer.asked) == len([p for p in result.pages if not p.error])


# --- with a real browser --------------------------------------------------

@needs_browser
async def test_a_real_browser_finds_the_javascript_written_link(base, manifest):
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     renderer=PlaywrightRenderer()))
    assert set(manifest["expected_pages_rendered"]) <= result.paths(HOST)
    assert result.rendered_pages == [f"{base}/js-only"]


@needs_browser
async def test_the_link_is_invisible_without_a_browser(base, manifest):
    """The control. /js-only/child has exactly one inbound link and JavaScript
    writes it, so no amount of HTTP-level cleverness can reach it."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0))
    assert "/js-only/child" not in result.paths(HOST)
    assert set(manifest["expected_pages"]) <= result.paths(HOST)


@needs_browser
async def test_triage_is_an_order_of_magnitude_cheaper_than_rendering_everything(base):
    """Both find the same pages. One of them pays 25x the browser cost to do it."""
    triaged = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                      renderer=PlaywrightRenderer()))
    everything = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                         renderer=PlaywrightRenderer(),
                                         render_everything=True))
    assert triaged.paths(HOST) == everything.paths(HOST)
    assert len(everything.rendered_pages) > 20
    assert everything.render_seconds > triaged.render_seconds * 5
