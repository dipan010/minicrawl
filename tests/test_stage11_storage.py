"""Stage 11 — what the crawl leaves behind.

Two claims to pin: content addressing makes deduplication a property of the
naming scheme rather than a feature, and the WARC we emit is readable by a
tool that knows nothing about this project.
"""
import gzip
import zlib

import pytest
from warcio.archiveiterator import ArchiveIterator

from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.fetch import Fetched
from minicrawl.freshness import FreshnessStore
from minicrawl.store import ContentStore, NullStore, digest_of
from minicrawl.warc import DROP_HEADERS, WarcWriter, http_block

HOST = "127.0.0.1:8081"


def response(url: str, body: bytes, status: int = 200, **kwargs) -> Fetched:
    return Fetched(url=url, final_url=kwargs.pop("final_url", url), status=status,
                   content_type="text/html", body=body,
                   headers=kwargs.pop("headers", {"content-type": "text/html"}),
                   raw_headers=kwargs.pop("raw_headers",
                                          [(b"Content-Type", b"text/html")]),
                   **kwargs)


@pytest.fixture
def store(tmp_path):
    s = ContentStore(tmp_path / "store")
    yield s
    s.close()


# --- content addressing ---------------------------------------------------

def test_identical_bodies_become_one_object(store):
    """Deduplication is not a step that runs. It is what naming an object after
    its own hash means."""
    first = store.put(response("http://h/a", b"same bytes"))
    second = store.put(response("http://h/a/", b"same bytes"))
    assert first.digest == second.digest
    assert first.new_object is True and second.new_object is False
    assert store.record_count == 2 and store.object_count == 1
    assert store.bytes_deduplicated == len(b"same bytes")


def test_different_bodies_stay_separate(store):
    store.put(response("http://h/a", b"one"))
    store.put(response("http://h/b", b"two"))
    assert store.object_count == 2


def test_stored_bytes_round_trip_exactly(store):
    body = bytes(range(256)) * 4
    store.put(response("http://h/bin", body))
    assert store.get("http://h/bin") == body


def test_the_object_name_is_the_hash_of_its_content(store):
    stored = store.put(response("http://h/a", b"payload"))
    assert stored.digest == digest_of(b"payload")
    assert store.path_for(stored.digest).read_bytes() == b"payload"


def test_every_url_that_served_the_bytes_is_recoverable(store):
    for url in ("http://h/a", "http://h/a/", "http://h/r"):
        store.put(response(url, b"one document"))
    digest = digest_of(b"one document")
    assert store.urls_for(digest) == ["http://h/a", "http://h/a/", "http://h/r"]
    assert store.duplicate_groups() == [(digest, 3)]


def test_refetching_a_url_updates_its_record_not_the_object_count(store):
    store.put(response("http://h/v", b"first"))
    store.put(response("http://h/v", b"second"))
    assert store.record_count == 1
    assert store.object_count == 2          # both versions kept, one record
    assert store.get("http://h/v") == b"second"


# --- the two refusals, both of which look like nothing --------------------

def test_a_304_is_refused(store):
    assert store.put(response("http://h/a", b"", status=304)).refused == "not_modified"
    assert store.object_count == 0


def test_a_truncated_body_is_refused(store):
    """A digest of a fragment claims a completeness the bytes do not have."""
    assert store.put(response("http://h/big", b"abcd", cap=4)).refused == "truncated"
    assert store.object_count == 0


def test_an_error_response_is_refused(store):
    assert store.put(response("http://h/x", b"", status=500)).refused == "not_ok"


def test_the_null_store_stores_nothing():
    assert NullStore().put(response("http://h/a", b"x")).ok is False
    assert NullStore().get("http://h/a") is None


# --- the failure the whole stage is shaped around -------------------------

async def test_a_second_crawl_does_not_empty_the_store(base, tmp_path):
    """With --freshness a second crawl is mostly 304s. If a bodyless response
    reached the store, run two would silently replace every archived page with
    zero bytes — and a missing thing looks like nothing at all."""
    store = ContentStore(tmp_path / "s")
    freshness = FreshnessStore(tmp_path / "f.sqlite3")
    config = dict(seeds=[f"{base}/"], max_depth=10, max_delay=0.0)

    first = await crawl(CrawlConfig(**config, store=store, freshness=freshness))
    snapshot = {url: store.get(url) for url in
                (p.url for p in first.pages if not p.error)}
    assert snapshot and all(v for v in snapshot.values())

    second = await crawl(CrawlConfig(**config, store=store, freshness=freshness))
    assert len(second.not_modified) > 15                   # the cache really did work
    assert second.store_refusals.get("not_modified", 0) > 15

    # Nothing may come back empty...
    for url in snapshot:
        assert store.get(url), f"{url} was emptied by the second crawl"
    # ...and anything the server said was unchanged must be byte-identical.
    # /volatile is excluded by construction rather than by exception: it never
    # answers 304 because it really does change every request.
    for url in second.not_modified:
        if url in snapshot:
            assert store.get(url) == snapshot[url], f"{url} changed under a 304"
    store.close()
    freshness.close()


# --- in a real crawl ------------------------------------------------------

@pytest.fixture(scope="module")
async def crawled(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage11")
    store = ContentStore(root / "store")
    warc = WarcWriter(root / "crawl.warc.gz")
    result = await crawl(CrawlConfig(seeds=[f"http://{HOST}/"], max_depth=10,
                                     max_delay=0.0, store=store, warc=warc))
    yield result, store, root / "crawl.warc.gz"
    store.close()


async def test_a_crawl_stores_fewer_objects_than_urls(crawled):
    result, store, _ = crawled
    assert store.object_count < store.record_count
    assert result.bytes_deduplicated > 0


async def test_the_declared_exact_pair_shares_one_object(crawled, manifest):
    _, store, _ = crawled
    a, b = manifest["dup_pairs"]["exact"][0]
    assert store.get(f"http://{HOST}{a}") == store.get(f"http://{HOST}{b}")
    shared = {tuple(sorted(store.urls_for(d))) for d, _ in store.duplicate_groups()}
    assert any({f"http://{HOST}{a}", f"http://{HOST}{b}"} <= set(group)
               for group in shared)


async def test_the_four_routes_to_a_collapse_to_one_object(crawled):
    """/a, /a/ (301), /a?a=1&b=2 (same content) and the end of the /r/1 chain.
    Stages 5 and 6 identified them; storage is where it finally costs nothing."""
    _, store, _ = crawled
    routes = {f"http://{HOST}{p}" for p in ("/a", "/a/", "/a?a=1&b=2", "/r/1")}
    digests = {store._db.execute("SELECT digest FROM records WHERE url=?",
                                 (url,)).fetchone() for url in routes}
    assert len({d for d in digests if d}) == 1


# --- WARC, validated by a reader that knows nothing about us --------------

async def test_warcio_reads_every_record(crawled):
    result, _, path = crawled
    with open(path, "rb") as fh:
        records = [(r.rec_type, r.rec_headers.get_header("WARC-Target-URI"))
                   for r in ArchiveIterator(fh)]
    assert records[0][0] == "warcinfo"
    responses = [r for r in records if r[0] == "response"]
    assert len(responses) == result.warc_records - 1
    assert all(uri for _, uri in responses)


async def test_every_record_declares_the_length_it_holds(crawled):
    """The bug this stage exists around: httpx decodes gzip, so the body no
    longer matches the Content-Length the server sent. Copy that header through
    and every reader mis-reads the record."""
    _, _, path = crawled
    with open(path, "rb") as fh:
        for record in ArchiveIterator(fh):
            if record.rec_type != "response":
                continue
            declared = record.http_headers.get_header("Content-Length")
            assert int(declared) == len(record.content_stream().read())


async def test_the_compressed_page_is_archived_decoded_and_says_so(crawled, manifest):
    _, _, path = crawled
    target = f"http://{HOST}{manifest['content_encoded'][0]}"
    with open(path, "rb") as fh:
        for record in ArchiveIterator(fh):
            if record.rec_headers.get_header("WARC-Target-URI") != target:
                continue
            assert record.rec_headers.get_header("X-Minicrawl-Decoded") == "gzip"
            assert record.http_headers.get_header("Content-Encoding") is None
            assert record.content_stream().read().startswith(b"<!doctype html>")
            return
    pytest.fail("the content-encoded page never reached the archive")


def test_transfer_headers_are_dropped_not_copied():
    fetched = response(
        "http://h/a", b"body",
        headers={"content-encoding": "gzip"},
        raw_headers=[(b"Content-Type", b"text/html"), (b"Content-Encoding", b"gzip"),
                     (b"Content-Length", b"11"), (b"Connection", b"keep-alive")])
    block = http_block(fetched)
    head = block.split(b"\r\n\r\n", 1)[0].lower()
    for dropped in DROP_HEADERS - {b"content-length"}:
        assert dropped not in head
    assert b"content-length: 4" in head      # re-derived from the body we hold


def test_header_order_and_casing_survive():
    """The lowercased dict every other stage uses would silently normalise an
    archive. raw_headers exists so it does not."""
    fetched = response("http://h/a", b"x",
                       raw_headers=[(b"X-Zebra", b"1"), (b"Content-Type", b"text/html"),
                                    (b"X-Apple", b"2")])
    head = http_block(fetched).split(b"\r\n\r\n", 1)[0]
    assert head.index(b"X-Zebra") < head.index(b"Content-Type") < head.index(b"X-Apple")


def test_each_record_is_an_independent_gzip_member(tmp_path):
    """Members, not one stream. It is what lets a reader seek to a record in a
    hundred-gigabyte file without decompressing what came before."""
    path = tmp_path / "members.warc.gz"
    writer = WarcWriter(path)
    for n in range(3):
        writer.write_response(response(f"http://h/{n}", b"body %d" % n))
    writer.close()

    raw, members = path.read_bytes(), 0
    while raw:
        decompressor = zlib.decompressobj(31)
        decompressor.decompress(raw)
        members += 1
        raw = decompressor.unused_data
    assert members == 4                       # warcinfo + three responses


def test_warc_refuses_what_the_store_refuses(tmp_path):
    writer = WarcWriter(tmp_path / "r.warc.gz")
    assert writer.write_response(response("http://h/a", b"", status=304)) == "not_modified"
    assert writer.write_response(response("http://h/b", b"ab", cap=2)) == "truncated"
    assert writer.records == 0
    assert not (tmp_path / "r.warc.gz").exists()   # nothing archived, no file
    writer.close()


def test_an_empty_crawl_leaves_no_file(tmp_path):
    writer = WarcWriter(tmp_path / "empty.warc.gz")
    writer.close()
    assert not (tmp_path / "empty.warc.gz").exists()
