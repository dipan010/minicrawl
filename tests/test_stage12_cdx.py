"""Stage 12 — an archive you can actually use.

Three claims to pin:
  1. SURT is a total, stable order, including for the IP:port the corpus uses.
  2. Lookup really is a binary search over the FILE — proved by counting lines
     read, not by the answer coming back right.
  3. Replay re-derives the crawl's link graph with the origin server DEAD.
"""
import json
import os
import signal
import subprocess
import sys

import pytest

from minicrawl.cdx import CdxIndex, CdxRecord, surt, write_cdxj
from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.replay import ArchiveReplay, WarcReader
from minicrawl.warc import WarcWriter
from minicrawl.fetch import Fetched
from testsite import spec

HOST = "127.0.0.1:8081"
SEED = f"http://{HOST}/"


def response(url: str, body: bytes, **kwargs) -> Fetched:
    return Fetched(url=url, final_url=url, status=200, content_type="text/html",
                   body=body, headers={"content-type": "text/html"},
                   raw_headers=[(b"Content-Type", b"text/html")],
                   http_version="HTTP/1.1", reason_phrase="OK", **kwargs)


# --- SURT ------------------------------------------------------------------

def test_surt_reverses_host_labels_so_a_domain_is_contiguous():
    assert surt("http://www.example.com/a") == "com,example,www)/a"
    assert surt("http://example.com/a") == "com,example)/a"
    # Sorting those puts every example.com URL in one run of the file, which
    # is the entire reason for the transform.
    keys = sorted([surt("http://www.example.com/a"), surt("http://other.org/z"),
                   surt("http://example.com/b")])
    assert keys[0].startswith("com,example") and keys[1].startswith("com,example")


def test_surt_does_not_reverse_an_ip_literal():
    # `1,0,0,127` would sort this address beside unrelated hosts sharing a last
    # octet, and invents a hierarchy that does not exist. The corpus is served
    # entirely from IP:port, so this is the case that matters here.
    assert surt("http://127.0.0.1:8081/a") == "127.0.0.1:8081)/a"


def test_surt_keeps_the_port_so_two_corpus_hosts_never_collide():
    assert surt("http://127.0.0.1:8081/a") != surt("http://127.0.0.1:8082/a")


def test_surt_is_stable_under_the_normalisation_the_crawler_already_did():
    assert surt("http://127.0.0.1:8081/a?b=2&c=3") == "127.0.0.1:8081)/a?b=2&c=3"
    assert surt("http://127.0.0.1:8081") == "127.0.0.1:8081)/"


# --- the index is a file, and lookup is a seek -----------------------------

def make_index(tmp_path, n=4000):
    records = [CdxRecord(key=surt(f"http://{HOST}/p{i:05d}"), timestamp="20260101000000",
                         url=f"http://{HOST}/p{i:05d}", mime="text/html", status=200,
                         digest="sha256:x", offset=i * 100, length=100,
                         filename="a.warc.gz")
               for i in range(n)]
    path = tmp_path / "index.cdxj"
    write_cdxj(records, path)
    return path, n


def test_cdxj_is_written_sorted(tmp_path):
    unsorted = [
        CdxRecord(surt(f"http://{HOST}/z"), "20260101000000", f"http://{HOST}/z",
                  "text/html", 200, "sha256:x", 0, 1, "a.warc.gz"),
        CdxRecord(surt(f"http://{HOST}/a"), "20260101000000", f"http://{HOST}/a",
                  "text/html", 200, "sha256:x", 1, 1, "a.warc.gz"),
    ]
    path = tmp_path / "i.cdxj"
    write_cdxj(unsorted, path)
    keys = [line.split(" ")[0] for line in path.read_text().splitlines()]
    assert keys == sorted(keys)


def test_lookup_reads_a_handful_of_lines_not_the_file(tmp_path):
    """The claim under test is the ALGORITHM, not the answer.

    A linear scan returns the right record too. What separates a binary search
    from a scan is how much of the file it touched, so that is what is
    asserted: log2(4000) is about 12, and a scan would read thousands.
    """
    path, n = make_index(tmp_path, 4000)
    index = CdxIndex(path)
    hit = index.lookup(f"http://{HOST}/p03999")
    assert len(hit) == 1 and hit[0].offset == 3999 * 100
    assert index.lines_read < 30, f"read {index.lines_read} lines — that is a scan"


def test_lookup_finds_the_first_and_last_lines(tmp_path):
    path, _ = make_index(tmp_path, 500)
    index = CdxIndex(path)
    assert index.lookup(f"http://{HOST}/p00000")[0].offset == 0
    assert index.lookup(f"http://{HOST}/p00499")[0].offset == 499 * 100


def test_lookup_of_a_url_that_was_never_archived_is_empty(tmp_path):
    path, _ = make_index(tmp_path, 500)
    assert CdxIndex(path).lookup(f"http://{HOST}/nope") == []


def test_every_url_in_the_index_is_findable(tmp_path):
    """A binary search that is subtly wrong finds most keys. Check all of them."""
    path, n = make_index(tmp_path, 1000)
    index = CdxIndex(path)
    for i in range(n):
        found = index.lookup(f"http://{HOST}/p{i:05d}")
        assert len(found) == 1 and found[0].offset == i * 100, f"lost p{i:05d}"


def test_repeated_captures_of_one_url_come_back_oldest_first(tmp_path):
    base = dict(mime="text/html", status=200, digest="sha256:x", length=10,
                filename="a.warc.gz")
    url = f"http://{HOST}/a"
    write_cdxj([CdxRecord(surt(url), "20260102000000", url, offset=20, **base),
                CdxRecord(surt(url), "20260101000000", url, offset=10, **base)],
               tmp_path / "i.cdxj")
    got = CdxIndex(tmp_path / "i.cdxj").lookup(url)
    assert [r.timestamp for r in got] == ["20260101000000", "20260102000000"]


def test_prefix_query_returns_one_contiguous_run(tmp_path):
    records = [CdxRecord(surt(u), "20260101000000", u, "text/html", 200,
                         "sha256:x", 0, 1, "a.warc.gz")
               for u in [f"http://{HOST}/docs/one", f"http://{HOST}/docs/two",
                         f"http://{HOST}/a", f"http://{HOST}/zzz"]]
    write_cdxj(records, tmp_path / "i.cdxj")
    hits = CdxIndex(tmp_path / "i.cdxj").prefix(f"{HOST})/docs/")
    assert sorted(r.url for r in hits) == [f"http://{HOST}/docs/one",
                                           f"http://{HOST}/docs/two"]


# --- offsets agree with a tool that knows nothing about us -----------------

def test_offsets_match_warcio_index(tmp_path):
    """warcio is the independent validator, as at stage 11.

    Every record is compared, not a spot check: an error in the warcinfo
    record's own offset shifts nothing after it, so checking only the first
    hit would pass while the file was wrong.
    """
    path = tmp_path / "a.warc.gz"
    writer = WarcWriter(path)
    for i in range(6):
        writer.write_response(response(f"http://{HOST}/p{i}",
                                       b"<html>" + b"x" * (i * 37) + b"</html>"))
    writer.close()

    out = subprocess.run([sys.executable, "-m", "warcio.cli", "index", "-f",
                          "offset,length,warc-type", str(path)],
                         capture_output=True, text=True, check=True).stdout
    theirs = [(int(r["offset"]), int(r["length"]))
              for r in map(json.loads, out.strip().splitlines())
              if r["warc-type"] == "response"]
    assert [(r.offset, r.length) for r in writer.index] == theirs


def test_reading_a_record_reads_only_its_own_bytes(tmp_path):
    path = tmp_path / "a.warc.gz"
    writer = WarcWriter(path)
    for i in range(20):
        writer.write_response(response(f"http://{HOST}/p{i}", b"<html>%d</html>" % i))
    writer.close()

    reader = WarcReader(path)
    target = writer.index[17]
    got = reader.read(target)
    assert got.body == b"<html>17</html>"
    # The seek is the point: it touched one member, not the file.
    assert reader.bytes_read == target.length
    assert reader.bytes_read < path.stat().st_size / 4


def test_a_record_round_trips_headers_and_status(tmp_path):
    path = tmp_path / "a.warc.gz"
    writer = WarcWriter(path)
    fetched = Fetched(url=f"http://{HOST}/a", final_url=f"http://{HOST}/a",
                      status=200, content_type="text/html", body=b"<html>hi</html>",
                      headers={"content-type": "text/html", "x-thing": "kept"},
                      raw_headers=[(b"Content-Type", b"text/html"),
                                   (b"X-Thing", b"kept")],
                      http_version="HTTP/1.1", reason_phrase="OK")
    writer.write_response(fetched)
    writer.close()

    got = WarcReader(path).read(writer.index[0])
    assert got.status == 200 and got.reason_phrase == "OK"
    assert got.http_version == "HTTP/1.1"
    assert got.headers["x-thing"] == "kept"
    assert got.body == fetched.body
    assert got.url == fetched.final_url


def test_a_body_containing_a_blank_line_is_not_truncated(tmp_path):
    """Content-Length is authoritative; scanning for CRLFCRLF would cut here."""
    body = b"<html>\r\n\r\nafter the blank line</html>"
    path = tmp_path / "a.warc.gz"
    writer = WarcWriter(path)
    writer.write_response(response(f"http://{HOST}/a", body))
    writer.close()
    assert WarcReader(path).read(writer.index[0]).body == body


# --- replay, with the corpus server switched off ---------------------------

@pytest.fixture
def archived(tmp_path):
    """Crawl the live corpus once, keeping a WARC and an index."""
    warc_path, cdx_path = tmp_path / "c.warc.gz", tmp_path / "c.cdxj"
    writer = WarcWriter(warc_path)
    config = CrawlConfig(seeds=[SEED], max_pages=60, max_depth=4,
                         warc=writer, on_page=None)
    import asyncio
    result = asyncio.run(crawl(config))
    write_cdxj(writer.index, cdx_path)
    return result, ArchiveReplay(warc_path, cdx_path)


def test_replay_re_derives_the_link_graph_with_no_network(archived, monkeypatch):
    """The headline claim of the stage.

    Every socket call is poisoned for the duration, so any attempt to reach the
    origin raises instead of quietly succeeding against a server that happens
    to still be running. A crawl that needs the network cannot pass this by
    accident.
    """
    import socket
    live, replay = archived

    def no_network(*args, **kwargs):
        raise AssertionError("replay touched the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    checked = 0
    for page in live.pages:
        if page.error or not page.n_links:
            continue
        links = replay.links(page.url)
        if links is None:
            continue
        assert len(links) == page.n_links, f"{page.url}: link count changed"
        checked += 1
    assert checked >= 10, f"only {checked} pages compared — the archive is thin"


def test_replay_serves_a_page_the_crawl_actually_saw(archived):
    live, replay = archived
    got = replay.fetch(f"http://{HOST}/a")
    assert got is not None and got.status == 200
    assert b"<html" in got.body.lower()


def test_replay_of_a_url_never_crawled_is_none(archived):
    _, replay = archived
    assert replay.fetch(f"http://{HOST}/never-existed") is None


def test_the_compressed_page_replays_without_being_gunzipped(archived):
    """`/compressed` was archived DECODED with Content-Encoding stripped.

    A reader that decompresses on sight corrupts it. The record admits what
    was done in X-Minicrawl-Decoded, and the body must already be readable.
    """
    _, replay = archived
    got = replay.fetch(f"http://{HOST}/compressed")
    assert got is not None
    assert got.decoded_by_crawler == "gzip"
    assert "content-encoding" not in got.headers
    assert b"<html" in got.body.lower()
    assert int(got.headers["content-length"]) == len(got.body)


def test_every_archived_url_is_findable_through_the_index(archived):
    live, replay = archived
    for url in replay.urls():
        assert replay.fetch(url) is not None, f"{url} indexed but not readable"


def test_volatile_is_excluded_by_construction_not_by_exception(archived):
    """`/volatile` changes on every request, so an archived copy and a fresh
    fetch cannot agree. The comparison above compares link COUNTS between the
    live crawl and the archive of that same crawl — one capture, compared with
    itself — rather than archive against a new fetch. That is why no exception
    list is needed, and this test exists to keep it that way."""
    _, replay = archived
    got = replay.fetch(f"http://{HOST}/volatile")
    assert got is None or got.status == 200
