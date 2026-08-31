"""Stage 1 — a single request, and links out of a single page."""
import pytest

from minicrawl import extract, fetch


@pytest.fixture
async def client():
    c = fetch.make_client(timeout=3.0)
    yield c
    await c.aclose()


async def test_fetches_html(client, base):
    got = await fetch.fetch(client, f"{base}/a")
    assert got.ok and got.is_html
    assert b"Politeness is the part of crawling" in got.body


async def test_follows_a_redirect_chain(client, base):
    got = await fetch.fetch(client, f"{base}/r/1")
    assert got.ok
    assert got.final_url == f"{base}/a"
    assert len(got.redirects) == 3          # /r/1 -> /r/2 -> /r/3 -> /a


async def test_redirect_loop_is_an_error_not_a_hang(client, base):
    got = await fetch.fetch(client, f"{base}/loop/1")
    assert got.error == "redirect_loop"


async def test_slow_endpoint_times_out(base):
    client = fetch.make_client(timeout=0.5)
    try:
        got = await fetch.fetch(client, f"{base}/hang")
        assert got.error == "timeout"
    finally:
        await client.aclose()


async def test_non_html_is_labelled_not_parsed(client, base):
    got = await fetch.fetch(client, f"{base}/files/report.pdf")
    assert got.ok and not got.is_html
    assert got.content_type == "application/pdf"


async def test_body_cap_stops_a_huge_response(client, base):
    got = await fetch.fetch(client, f"{base}/gen/1", max_bytes=4096)
    assert got.truncated and len(got.body) == 4096


# --- extraction -----------------------------------------------------------

def parse_path(base, path):
    from testsite.server import render_page
    from testsite import spec
    return extract.parse(render_page(path, spec.PRIMARY).encode(), f"{base}{path}")


def test_base_href_changes_relative_resolution(base):
    got = parse_path(base, "/docs/")
    assert got.links == [f"{base}/docs/one", f"{base}/docs/two", f"{base}/docs/sub/three"]


def test_protocol_relative_and_bare_relative_links(base):
    got = parse_path(base, "/a")
    assert got.links == [f"{base}/b", f"{base}/c"]


def test_fragments_are_dropped_and_deduped(base, manifest):
    got = parse_path(base, "/variants")
    declared = sum(len(g["hrefs"]) for g in manifest["normalization_groups"])
    assert declared == 12
    assert len(manifest["redirect_normalized"]) == 1    # /a/ -> settled by a 301
    assert len(manifest["malformed_hrefs"]) == 1
    # Fragments collapse at extraction (/a#section and /a#other become /a).
    # The other spellings survive until stage 5 normalises them.
    assert f"{base}/a#section" not in got.links
    # 14 hrefs in the page -> 9 distinct links here: fragments, the bare "?" and
    # the repeated absolute form collapse at extraction, the malformed one is
    # dropped. Stage 5 takes these 9 down to 3 by normalisation, and then to 2
    # by following the 301 on /a/.
    assert len(got.links) == 9


def test_malformed_href_in_corpus_is_dropped(base, manifest):
    links = parse_path(base, "/variants").links
    for bad in manifest["malformed_hrefs"]:
        assert bad not in links


def test_canonical_link_is_extracted(base):
    assert parse_path(base, "/dup/canonical-source").canonical == f"{base}/a"


def test_malformed_href_is_dropped_not_raised():
    assert extract.absolutise("http://[::bad::]/x", "http://h/") is None
