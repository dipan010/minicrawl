"""Stage 13 — what the real web did to a crawler built against a clean corpus.

Two claims:
  1. Character encoding is DECIDED, by a stated precedence, and the decision is
     recorded. Twelve stages assumed UTF-8 because the corpus was all UTF-8.
  2. The browser front end is a view onto a Python crawl, and the crawl it
     shows is the same one the CLI runs — same config, same limits, minus the
     overrides a web form has no business offering.
"""
import asyncio
import codecs
import json

import httpx
import pytest

from minicrawl.charset import (FALLBACK, charset_from_header, charset_from_meta,
                               decode)
from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.extract import parse
from minicrawl.web import server as web

LATIN = "café naïve résumé"


# --- the precedence, rule by rule -----------------------------------------

def test_bom_wins_over_every_declaration():
    """A BOM is the bytes announcing themselves; a meta tag is what an author
    typed once and may have copied from somewhere else entirely."""
    body = codecs.BOM_UTF8 + '<meta charset="windows-1252">café'.encode("utf-8")
    text, encoding, source = decode(body, "text/html; charset=windows-1252")
    assert source == "bom" and encoding == "utf-8-sig"
    assert text.endswith("café")


def test_http_header_beats_the_document():
    """The server knows what it just encoded. The document is hearsay."""
    body = f'<meta charset="utf-8">{LATIN}'.encode("windows-1252")
    text, encoding, source = decode(body, "text/html; charset=windows-1252")
    assert (encoding, source) == ("windows-1252", "http header")
    assert text.endswith(LATIN)


def test_meta_is_used_when_the_header_says_nothing():
    body = f'<meta charset="windows-1252">{LATIN}'.encode("windows-1252")
    text, encoding, source = decode(body, "text/html")
    assert (encoding, source) == ("windows-1252", "meta tag")
    assert text.endswith(LATIN)


def test_a_declaration_that_does_not_decode_is_abandoned_not_forced():
    """The page claims UTF-8 and is windows-1252 — common on the real web.

    Decoding strictly is what makes the lie DETECTABLE. With errors="replace"
    the bad declaration would be honoured and the text quietly destroyed,
    which is precisely the failure this stage was opened to fix.
    """
    body = f'<meta charset="utf-8">{LATIN}'.encode("windows-1252")
    text, encoding, source = decode(body, "text/html; charset=utf-8")
    assert (encoding, source) == (FALLBACK, "fallback")
    assert text.endswith(LATIN)
    assert "\ufffd" not in text, "recovered text must not contain replacement chars"


def test_clean_utf8_is_still_utf8_when_nothing_declares_anything():
    text, encoding, source = decode(LATIN.encode("utf-8"), None)
    assert encoding == "utf-8" and source == "utf-8 decoded cleanly"
    assert text == LATIN


def test_an_encoding_name_python_does_not_have_is_just_another_dead_hint():
    """Real pages name encodings that do not exist. An unknown label is a hint
    that did not work out, not a crash."""
    text, encoding, _ = decode(LATIN.encode("windows-1252"),
                               "text/html; charset=utf8mb4-not-a-codec")
    assert encoding == FALLBACK and text.endswith(LATIN)


def test_the_fallback_can_never_itself_fail():
    """Every byte sequence is valid windows-1252, which is why it is the
    terminal rule rather than one more guess."""
    # Note what is NOT in this list: anything starting b"\xff\xfe". That is a
    # UTF-16 BOM, and being decoded as UTF-16 is correct, not a fallback.
    for body in (b"\x00\x81\x8d\x90", bytes(range(0x80, 0x100)),
                 b"\xe9\xe8\xea invalid utf-8"):
        text, encoding, _ = decode(body, "text/html; charset=utf-8")
        assert isinstance(text, str) and encoding == FALLBACK


def test_only_the_head_is_scanned_for_a_meta_charset():
    """A declaration must appear early. Scanning a whole document for one is
    how a parser becomes the slow part of a crawl."""
    buried = b"<html>" + b"x" * 9000 + b'<meta charset="windows-1252">'
    assert charset_from_meta(buried) is None


def test_header_and_meta_extraction_are_case_and_quote_insensitive():
    assert charset_from_header("TEXT/HTML; CHARSET=UTF-8") == "utf-8"
    assert charset_from_header("text/html; charset='iso-8859-1'") == "iso-8859-1"
    assert charset_from_meta(b"<META  CHARSET = 'ISO-8859-1'>") == "iso-8859-1"
    assert charset_from_header("text/html") is None


# --- against the corpus's declared ground truth ---------------------------

def test_every_non_utf8_page_decodes_to_its_real_text(base, manifest):
    """The corpus declares which pages are not UTF-8 and what they claim to
    be. Each must come back readable, whichever rule got there."""
    assert manifest["non_utf8"], "the corpus stopped serving non-UTF-8 pages"
    for path, declared in manifest["non_utf8"].items():
        response = httpx.get(base + path)
        found = parse(response.content, base + path,
                      response.headers.get("content-type"))
        assert found.encoding == declared["charset"], path
        assert "café" in found.title, f"{path}: title is {found.title!r}"
        assert "\ufffd" not in found.title, f"{path}: mojibake survived"


def test_the_decision_is_recorded_not_merely_correct(base, manifest):
    """A right answer and a lucky answer look identical unless the reason is
    written down."""
    sources = {}
    for path in manifest["non_utf8"]:
        response = httpx.get(base + path)
        found = parse(response.content, base + path,
                      response.headers.get("content-type"))
        sources[path] = found.encoding_source
    # The corpus deliberately exercises different rules, not one rule thrice.
    assert len(set(sources.values())) > 1, sources
    assert sources["/encoded/undeclared"] == "fallback"


async def test_a_crawl_reads_the_encoded_pages_correctly(base, manifest):
    result = await crawl(CrawlConfig(seeds=[base + "/"], max_pages=60,
                                     max_depth=3, on_page=None))
    titles = {p.url.replace(base, ""): p.title for p in result.pages}
    for path in manifest["non_utf8"]:
        assert path in titles, f"{path} was never crawled"
        assert "\ufffd" not in titles[path], f"{path}: {titles[path]!r}"


def test_links_survived_mojibake_which_is_why_this_went_unnoticed(base):
    """The bug hid for twelve stages because hrefs are ASCII: a page decoded
    wrongly still yields the right links, so the crawl LOOKS fine. Only the
    text is destroyed. Pinning that keeps the reason legible."""
    body = f'<a href="/x">l</a>{LATIN}'.encode("windows-1252")

    # What stage 1 through 12 did: hand the bytes over assuming UTF-8.
    old = parse(body.decode("utf-8", "replace").encode("utf-8"), "http://h/")
    assert old.links == ["http://h/x"]        # traversal unharmed — hence hidden
    assert "\ufffd" in old.text              # text destroyed

    # What it does now, from the same bytes plus the header that was always
    # there and never read.
    fixed = parse(body, "http://h/", "text/html; charset=windows-1252")
    assert fixed.links == old.links
    assert LATIN in fixed.text


# --- the web front end ----------------------------------------------------

def test_page_event_is_written_field_by_field():
    """Adding a field to `Page` must not silently change the wire format, so
    the event is built explicitly rather than from `asdict`."""
    from minicrawl.crawler import Page
    event = web.page_event(Page(url="http://h/a", final_url="http://h/a",
                                status=200, depth=1, title="t", n_links=3,
                                elapsed=0.5))
    assert set(event) == {"url", "final_url", "status", "depth", "title",
                          "links", "elapsed", "error", "verdict",
                          "duplicate_of", "words", "from_cache", "host"}
    assert event["host"] == "h"
    assert json.dumps(event)          # must be serialisable, always


async def test_crawl_events_streams_pages_then_exactly_one_summary(base):
    kinds = []
    async for kind, data in web.crawl_events(base + "/", max_pages=6, max_depth=2,
                                             workers=2, delay=0.5, same_host=True):
        kinds.append(kind)
        assert json.dumps(data)
    assert kinds[-1] == "done", "the stream must end with a terminal event"
    assert kinds.count("done") == 1
    assert kinds.count("page") >= 1


async def test_the_browser_cannot_switch_robots_off(base, monkeypatch):
    """The CLI has --ignore-robots because a human running it owns the
    consequences. A form on a web page does not get that option, so there is
    no parameter that reaches `respect_robots`."""
    captured = {}
    real = web.crawl

    async def spy(config):
        captured["config"] = config
        return await real(config)

    monkeypatch.setattr(web, "crawl", spy)
    async for _ in web.crawl_events(base + "/", 2, 1, 1, 0.5, True):
        pass
    assert captured["config"].respect_robots is True


async def test_the_form_cannot_raise_the_caps(base, monkeypatch):
    captured = {}
    real = web.crawl

    async def spy(config):
        captured["config"] = config
        return await real(config)

    monkeypatch.setattr(web, "crawl", spy)
    async for _ in web.crawl_events(base + "/", max_pages=99999, max_depth=1,
                                    workers=500, delay=0.0, same_host=True):
        pass
    config = captured["config"]
    assert config.max_pages == web.MAX_PAGES_CEILING
    assert config.workers == web.MAX_WORKERS_CEILING
    assert config.default_delay >= web.MIN_DELAY


async def test_the_server_speaks_sse_over_the_wire(base):
    """SSE framing is unforgiving: a missing blank line makes the browser wait
    forever and report nothing wrong — the same silent failure shape as the
    stage-10 Scrapy hooks. So this asserts the bytes, not a Python object.
    """
    port = web.free_port()
    server = await asyncio.start_server(web._handle, "127.0.0.1", port)
    try:
        async with httpx.AsyncClient() as client:
            home = await client.get(f"http://127.0.0.1:{port}/")
            assert home.status_code == 200
            assert home.headers["content-type"].startswith("text/html")
            assert (await client.get(f"http://127.0.0.1:{port}/nope")).status_code == 404

            events, url = [], f"http://127.0.0.1:{port}/crawl"
            async with client.stream("GET", url, params={
                    "url": base + "/", "max_pages": 3, "max_depth": 1,
                    "workers": 1, "delay": 0.5}, timeout=60) as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                assert "content-length" not in response.headers
                raw = ""
                async for chunk in response.aiter_text():
                    raw += chunk
            # Every message is `event: X\ndata: {...}` closed by a BLANK line.
            for block in filter(None, raw.split("\n\n")):
                lines = block.strip().split("\n")
                assert lines[0].startswith("event: ")
                assert lines[1].startswith("data: ")
                json.loads(lines[1][6:])
                events.append(lines[0][7:])
            assert events[0] == "started" and events[-1] == "done"
    finally:
        server.close()
        await server.wait_closed()


async def test_a_url_that_is_not_http_is_refused_before_any_fetch():
    port = web.free_port()
    server = await asyncio.start_server(web._handle, "127.0.0.1", port)
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/crawl",
                                     params={"url": "file:///etc/passwd"},
                                     timeout=20) as response:
                raw = "".join([chunk async for chunk in response.aiter_text()])
    finally:
        server.close()
        await server.wait_closed()
    assert "event: failed" in raw
    assert "event: page" not in raw


@pytest.mark.parametrize("seed", ["", "notaurl", "file:///etc/passwd",
                                  "javascript:alert(1)", "ftp://h/x"])
def test_only_http_and_https_seeds_are_accepted(seed):
    assert web._acceptable(seed) is False


# --- what this stage does NOT do ------------------------------------------

def test_characterises_no_encoding_detection_from_the_bytes_themselves():
    """When every declaration is absent or wrong, the fallback is windows-1252
    — the rule the HTML standard mandates, and wrong for most of the world.

    A page in Shift-JIS or KOI8-R that declares nothing decodes to plausible
    Latin nonsense rather than failing, because windows-1252 accepts every
    byte. Real crawlers add statistical detection (chardet, or ICU) to guess
    from byte frequencies. This asserts the shortcoming so it fails when that
    lands.
    """
    japanese = "日本語のページ"
    text, encoding, source = decode(japanese.encode("shift_jis"), "text/html")
    assert (encoding, source) == (FALLBACK, "fallback")
    assert text != japanese, "if this now round-trips, detection was added"
