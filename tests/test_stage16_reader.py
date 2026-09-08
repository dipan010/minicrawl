"""Stage 16 — reader mode, and the Markdown it produces.

Two claims: the conversion keeps a document's *structure* (which is what a
language model reads shape from), and a read is cheap because it does less
than a crawl, not because anything was optimised.
"""
import asyncio
import time

import pytest

from minicrawl.markdown import main_node, to_markdown
from minicrawl.reader import RobotsGate, ReadResult, read, read_many
from selectolax.lexbor import LexborHTMLParser

BASE = "https://h.example/dir/page"
SEED = "http://127.0.0.1:8081"


def md(html: str, base: str = BASE) -> str:
    return to_markdown(html, base)


# --- structure survives ----------------------------------------------------

def test_headings_keep_their_level():
    assert md("<article><h1>One</h1><h3>Three</h3></article>") == "# One\n\n### Three"


def test_emphasis_on_a_node_handed_over_directly_is_not_lost():
    """The bug this test exists for: a function that formats a node's CHILDREN
    silently drops the node's own emphasis when a block walker hands it a
    <strong> directly. The text is all there — just plain — so nothing looks
    broken. Hence a test per inline element rather than one for prose."""
    assert md("<article><p><strong>b</strong></p></article>") == "**b**"
    assert md("<article><p><em>i</em></p></article>") == "*i*"
    assert md("<article><p><code>c</code></p></article>") == "`c`"


def test_links_are_made_absolute():
    """A relative link in extracted Markdown looks usable and is not, which is
    worse than omitting it."""
    out = md('<article><p><a href="../up">u</a></p></article>')
    assert out == "[u](https://h.example/up)"


@pytest.mark.parametrize("href", ["javascript:alert(1)", "#anchor"])
def test_unusable_hrefs_keep_the_text_and_drop_the_link(href):
    assert md(f'<article><p><a href="{href}">label</a></p></article>') == "label"


def test_nested_lists_are_indented_not_flattened():
    out = md("<article><ul><li>a</li><li>b<ul><li>deep</li></ul></li></ul></article>")
    assert "- a" in out and "- b" in out
    assert "  - deep" in out, out


def test_ordered_lists_are_numbered_in_order():
    out = md("<article><ol><li>x</li><li>y</li><li>z</li></ol></article>")
    assert [line.strip() for line in out.splitlines() if line.strip()] == \
        ["1. x", "2. y", "3. z"]


def test_code_blocks_are_fenced_and_keep_their_content():
    out = md("<article><pre><code>def f():\n    return 1</code></pre></article>")
    assert out.startswith("```") and out.endswith("```")
    assert "    return 1" in out


def test_tables_get_a_separator_row():
    """A pipe table without a separator is not a table to anything that parses
    Markdown, so one is emitted even when the source has no <th>."""
    out = md("<article><table><tr><td>1</td><td>2</td></tr></table></article>")
    assert "| --- | --- |" in out


def test_a_pipe_inside_a_cell_is_escaped():
    out = md("<article><table><tr><td>a|b</td></tr></table></article>")
    assert r"a\|b" in out


def test_furniture_is_removed():
    out = md("<body><nav><a href='/x'>MENU</a></nav>"
             "<article><p>Real content here.</p></article>"
             "<footer>COPYRIGHT</footer></body>")
    assert "Real content" in out
    assert "MENU" not in out and "COPYRIGHT" not in out


def test_an_unknown_container_element_is_not_flattened():
    """Listing block tags rather than inline ones means any element the list
    has not heard of — a custom element, a <section> — collapses the document
    into one run-on paragraph. The allowlist is inline for that reason."""
    out = md("<article><my-block><h2>Head</h2></my-block>"
             "<my-block><p>Body.</p></my-block></article>")
    assert "## Head" in out and "\n" in out


# --- picking the document --------------------------------------------------

def test_an_article_element_wins_over_the_body():
    tree = LexborHTMLParser("<body><p>" + "junk " * 100 + "</p>"
                            "<article><p>" + "real " * 100 + "</p></article></body>")
    assert main_node(tree).tag == "article"


def test_an_empty_main_wrapper_is_not_believed():
    """Empty <main> wrappers are common. Picking one yields a blank read from
    a page that is full of words."""
    tree = LexborHTMLParser("<body><main></main><p>" + "words " * 200 + "</p></body>")
    assert main_node(tree).tag == "body"


def test_a_page_with_no_body_does_not_raise():
    assert md("<html></html>") == ""


# --- encodings survive the trip --------------------------------------------

def test_markdown_decodes_a_non_utf8_page():
    body = "<article><p>café naïve</p></article>".encode("windows-1252")
    assert "café naïve" in to_markdown(
        body, BASE, content_type="text/html; charset=windows-1252")


# --- reading ---------------------------------------------------------------

async def test_reading_one_page_returns_markdown(base):
    result = await read(base + "/a")
    assert result.ok and result.status == 200
    assert result.title == "Page A"
    assert result.markdown.startswith("# Page A")
    assert result.words > 0


async def test_robots_is_obeyed_by_default(base):
    result = await read(base + "/private/secret")
    assert result.error == "blocked by robots.txt"
    assert result.markdown == ""


async def test_robots_can_be_skipped_deliberately(base):
    result = await read(base + "/private/secret", respect_robots=False)
    assert result.ok, "the page exists; only robots was refusing it"


async def test_robots_is_fetched_once_per_host_not_once_per_url(base):
    """A read of many URLs across few hosts must pay for few robots files.
    That amortisation is the only reason checking robots is affordable here."""
    from minicrawl.fetch import make_client
    async with make_client() as client:
        gate = RobotsGate(client)
        await asyncio.gather(*(gate.allows(f"{base}/p{i}") for i in range(12)))
    assert gate.fetches == 1, f"fetched robots.txt {gate.fetches} times"


async def test_results_come_back_in_the_order_given(base):
    urls = [base + p for p in ("/c", "/a", "/b")]
    assert [r.url for r in await read_many(urls)] == urls


async def test_one_bad_url_does_not_lose_the_others(base):
    urls = [base + "/a", "http://127.0.0.1:9/nothing-listening", base + "/b"]
    results = await read_many(urls, timeout=2)
    assert results[0].ok and results[2].ok
    assert results[1].error is not None


async def test_a_non_html_response_is_reported_not_converted(base):
    result = await read(base + "/files/report.pdf", respect_robots=False)
    assert result.markdown == ""
    assert "not html" in result.error


async def test_a_404_is_an_error_with_its_status_kept(base):
    result = await read(base + "/nope")
    assert result.status == 404 and result.error == "http 404"


async def test_reading_is_faster_than_crawling_the_same_pages(base):
    """The claim of the stage, measured rather than asserted.

    A read of N pages on one host has no per-host queue; a crawl of the same N
    waits out Crawl-delay between every one. The gap is politeness, and it is
    large enough that a loose bound is still decisive.
    """
    from minicrawl.crawler import CrawlConfig, crawl as do_crawl
    paths = ["/", "/a", "/b", "/c", "/docs/one", "/docs/two"]

    started = time.perf_counter()
    results = await read_many([base + p for p in paths], concurrency=6)
    read_wall = time.perf_counter() - started
    assert all(r.ok for r in results)

    started = time.perf_counter()
    crawled = await do_crawl(CrawlConfig(seeds=[base + "/"], max_pages=len(paths),
                                         max_depth=2, workers=6,
                                         default_delay=0.2, on_page=None))
    crawl_wall = time.perf_counter() - started

    assert len(crawled.pages) == len(paths)
    assert read_wall < crawl_wall, f"read {read_wall:.2f}s vs crawl {crawl_wall:.2f}s"


def test_the_result_serialises_for_a_pipeline():
    payload = ReadResult(url="http://h/a", markdown="# x").to_dict()
    import json
    assert json.loads(json.dumps(payload))["markdown"] == "# x"


# --- the endpoint ----------------------------------------------------------

async def test_the_read_endpoint_obeys_robots(base):
    """`read_one` defaults `robots` to None, which is right for a library call
    the caller controls and wrong for an endpoint on a public host. The first
    version of /read forgot to pass a gate and served a disallowed page with a
    200 — no error anywhere, just a rule quietly not applied.
    """
    import httpx
    from minicrawl.web import server as web

    port = web.free_port()
    server = await asyncio.start_server(web._handle, "127.0.0.1", port)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            blocked = await client.get(f"http://127.0.0.1:{port}/read",
                                       params={"url": base + "/private/secret"})
            allowed = await client.get(f"http://127.0.0.1:{port}/read",
                                       params={"url": base + "/a"})
    finally:
        server.close()
        await server.wait_closed()

    assert blocked.status_code == 502
    assert blocked.json()["error"] == "blocked by robots.txt"
    assert allowed.status_code == 200 and allowed.json()["markdown"].startswith("# Page A")


async def test_the_read_endpoint_refuses_what_the_policy_refuses():
    import httpx
    from minicrawl.web import server as web

    port = web.free_port()
    server = await asyncio.start_server(web._handle, "127.0.0.1", port)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"http://127.0.0.1:{port}/read",
                                        params={"url": "file:///etc/passwd"})
    finally:
        server.close()
        await server.wait_closed()
    assert response.status_code == 400 and "error" in response.json()
