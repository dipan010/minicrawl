"""Stage 15 — getting the crawl out.

Two claims: the JSONL is something another program can consume without knowing
anything about this project, and the HTML report opens with no network at all.
"""
import json
import re
import zipfile

import pytest

from minicrawl.crawler import CrawlConfig, Page, crawl
from minicrawl.dedup import DuplicateIndex
from minicrawl.export import (JsonlExporter, MAX_TEXT, crawl_summary,
                              page_record, write_bundle, write_report)
from minicrawl.extract import parse
from minicrawl.warc import WarcWriter

SEED = "http://127.0.0.1:8081/"


def a_page(**kwargs) -> Page:
    defaults = dict(url="http://h/a", final_url="http://h/a", status=200,
                    depth=1, title="A", n_links=3, elapsed=0.25)
    return Page(**{**defaults, **kwargs})


# --- the record ------------------------------------------------------------

def test_the_record_carries_the_boilerplate_stripped_text_not_the_raw_body():
    """Exporting `text` instead of `main_text` ships the navigation on every
    page, which poisons anything built from the export — an index would score
    every page as being about the site's own menu."""
    found = parse(b"<html><body><nav><a href='/x'>MENU</a></nav>"
                  b"<article>The actual content of the page.</article></body></html>",
                  "http://h/a")
    record = page_record(a_page(), found)
    assert "actual content" in record["text"]
    assert "MENU" not in record["text"]


def test_a_page_with_no_parse_still_becomes_a_record():
    """A PDF is a crawled page. Dropping it would make the export disagree
    with the page count the crawl reported."""
    record = page_record(a_page(title=""), None)
    assert record["url"] == "http://h/a" and record["text"] is None


def test_runaway_text_is_capped():
    class Huge:
        main_text = "x" * (MAX_TEXT * 2)
        encoding = "utf-8"
    assert len(page_record(a_page(), Huge())["text"]) == MAX_TEXT


def test_the_record_is_written_field_by_field():
    """Adding a field to `Page` must not silently change a format other
    programs parse, so the keys are asserted exactly."""
    assert set(page_record(a_page(), None)) == {
        "url", "final_url", "status", "depth", "title", "links", "words",
        "verdict", "duplicate_of", "elapsed", "error", "from_cache",
        "rendered", "text", "encoding"}


def test_metadata_only_export_omits_the_text_fields():
    record = page_record(a_page(), None, text=False)
    assert "text" not in record and "encoding" not in record


# --- the file --------------------------------------------------------------

def test_every_line_is_one_valid_json_object(tmp_path):
    exporter = JsonlExporter(tmp_path / "out.jsonl")
    for i in range(5):
        exporter.record(a_page(url=f"http://h/{i}", final_url=f"http://h/{i}"), None)
    exporter.close()
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert [json.loads(line)["url"] for line in lines] == [f"http://h/{i}" for i in range(5)]


def test_text_containing_newlines_stays_on_one_line(tmp_path):
    """JSONL means one record per line. A page whose text contains a newline
    must not split into two records — json.dumps escapes it, and this asserts
    nobody 'simplifies' that away."""
    class Multi:
        main_text = "first line\nsecond line\r\nthird"
        encoding = "utf-8"
    exporter = JsonlExporter(tmp_path / "out.jsonl")
    exporter.record(a_page(), Multi())
    exporter.close()
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "second line" in json.loads(lines[0])["text"]


def test_non_ascii_text_survives_the_round_trip(tmp_path):
    class Accented:
        main_text = "café naïve 日本語"
        encoding = "windows-1252"
    exporter = JsonlExporter(tmp_path / "out.jsonl")
    exporter.record(a_page(), Accented())
    exporter.close()
    record = json.loads((tmp_path / "out.jsonl").read_text(encoding="utf-8"))
    assert record["text"] == "café naïve 日本語"


def test_configuring_an_exporter_that_never_runs_leaves_no_file(tmp_path):
    """The same lazy-open rule WarcWriter follows: an empty crawl should not
    litter the directory with empty files."""
    JsonlExporter(tmp_path / "unused.jsonl").close()
    assert not (tmp_path / "unused.jsonl").exists()


# --- the report ------------------------------------------------------------

def report_data(path):
    found = re.search(r"const DATA = (\{.*?\});", path.read_text(), re.S)
    return json.loads(found.group(1))


async def test_the_report_holds_the_crawl_it_reports_on(tmp_path):
    result = await crawl(CrawlConfig(seeds=[SEED], max_pages=12, max_depth=2,
                                     dedup=DuplicateIndex(), on_page=None))
    path = write_report(result, tmp_path / "r.html", [SEED])
    data = report_data(path)
    assert data["summary"]["pages"] == len(result.pages)
    assert len(data["pages"]) == len(result.pages)
    assert data["summary"]["seeds"] == [SEED]


async def test_the_report_needs_no_network_to_open(tmp_path):
    """A downloaded report is read offline. One that fetches a webfont renders
    wrong exactly where it is most likely to be opened."""
    result = await crawl(CrawlConfig(seeds=[SEED], max_pages=5, on_page=None))
    html = write_report(result, tmp_path / "r.html", [SEED]).read_text()
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    # The one link out is the project's own repository, in the footer, which
    # is a link to click rather than a resource to load.
    assert all("github.com/dipan010/minicrawl" in url for url in external), external
    assert "fonts.googleapis" not in html and "cdn" not in html


def test_the_report_template_ships_with_the_package():
    from minicrawl.export import TEMPLATE
    assert TEMPLATE.exists(), "report.html must be installed alongside the code"


async def test_the_summary_counts_agree_with_the_crawl(tmp_path):
    result = await crawl(CrawlConfig(seeds=[SEED], max_pages=20, max_depth=2,
                                     dedup=DuplicateIndex(), on_page=None))
    summary = crawl_summary(result, [SEED])
    assert summary["pages"] == len(result.pages)
    assert summary["errors"] == len(result.errors)
    assert summary["dedup"] == dict(result.dedup_counts)


# --- the bundle ------------------------------------------------------------

def test_the_bundle_holds_what_exists_and_skips_what_does_not(tmp_path):
    present = tmp_path / "a.jsonl"
    present.write_text("{}\n")
    path = write_bundle(tmp_path / "b.zip", {
        "pages.jsonl": present,
        "crawl.warc.gz": tmp_path / "missing.warc.gz",
        "report.html": None,
    })
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["pages.jsonl"]


def test_the_bundle_is_a_readable_zip(tmp_path):
    member = tmp_path / "pages.jsonl"
    member.write_text('{"url":"http://h/a"}\n')
    path = write_bundle(tmp_path / "b.zip", {"pages.jsonl": member})
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read("pages.jsonl"))["url"] == "http://h/a"


# --- through a real crawl --------------------------------------------------

async def test_a_crawl_exports_one_record_per_page_it_reported(tmp_path):
    exporter = JsonlExporter(tmp_path / "out.jsonl")
    result = await crawl(CrawlConfig(seeds=[SEED], max_pages=20, max_depth=3,
                                     dedup=DuplicateIndex(), exporter=exporter,
                                     on_page=None))
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(result.pages), "export and report must agree"
    exported = {json.loads(line)["final_url"] for line in lines}
    assert exported == {p.final_url for p in result.pages}


async def test_the_non_utf8_pages_export_as_readable_text(tmp_path, manifest, base):
    """Stage 13's fix has to survive the trip out. Mojibake in an export is
    worse than mojibake on screen: it is now someone else's input."""
    exporter = JsonlExporter(tmp_path / "out.jsonl")
    await crawl(CrawlConfig(seeds=[base + "/"], max_pages=40, max_depth=3,
                            exporter=exporter, on_page=None))
    records = {json.loads(l)["final_url"].replace(base, ""): json.loads(l)
               for l in (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()}
    for path in manifest["non_utf8"]:
        assert path in records, path
        assert "café" in records[path]["title"]
        assert "�" not in (records[path]["text"] or "")


async def test_a_304_is_exported_with_no_text_rather_than_dropped(tmp_path):
    """A second crawl answered 304 has no body and therefore no text. Dropping
    those pages would make the export shrink while the page count stayed the
    same — the shape of bug this project keeps finding."""
    from minicrawl.freshness import FreshnessStore
    fresh = FreshnessStore(tmp_path / "f.sqlite3")
    seeds, common = [SEED], dict(max_pages=8, max_depth=1, on_page=None)
    await crawl(CrawlConfig(seeds=seeds, freshness=fresh, **common))

    exporter = JsonlExporter(tmp_path / "second.jsonl")
    result = await crawl(CrawlConfig(seeds=seeds, freshness=fresh,
                                     exporter=exporter, **common))
    fresh.close()
    lines = (tmp_path / "second.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(result.pages)
    if result.not_modified:
        cached = [json.loads(l) for l in lines if json.loads(l)["from_cache"]]
        assert cached, "the 304s must still appear in the export"
        assert all(record["text"] is None for record in cached)
