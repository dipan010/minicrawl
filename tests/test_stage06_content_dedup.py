"""Stage 6 — main text, and deciding when two pages are the same page."""
from itertools import combinations

import pytest

from minicrawl import dedup
from minicrawl.crawler import CrawlConfig, crawl
from minicrawl.dedup import (BANDS, DuplicateIndex, Verdict, content_hash, hamming,
                             shingles, simhash)
from minicrawl.extract import parse
from testsite import spec
from testsite.server import render_page

HOST = "127.0.0.1:8081"


def text_of(path: str) -> str:
    return parse(render_page(path, 8081).encode(), f"http://{HOST}{path}").main_text


def generated(n: int) -> str:
    para = f"<p>{spec.GEN_FILLER_BLOCK}</p>"
    body = para * (spec.GEN_BODY_BYTES // len(para) + 1)
    return parse(f"<html><body><h1>Generated {n}</h1>{body}</body></html>".encode(),
                 "http://h").main_text


# --- boilerplate removal --------------------------------------------------

def test_furniture_is_stripped_from_main_text():
    """Navigation is identical on every page. Left in, it makes every page look
    similar to every other — exactly the signal dedup is trying to read."""
    got = parse(render_page("/a", 8081).encode(), f"http://{HOST}/a")
    assert "127.0.0.1:8081/b" not in got.main_text     # the <nav> is gone
    assert "127.0.0.1:8081/b" in got.text              # but not from raw body text


def test_links_survive_boilerplate_removal():
    """main_text() mutates the tree, so it must run after link extraction —
    the links live in the <nav> it deletes."""
    got = parse(render_page("/a", 8081).encode(), f"http://{HOST}/a")
    assert len(got.links) == 2


def test_scripts_are_not_content():
    got = parse(render_page("/js-only", 8081).encode(), f"http://{HOST}/js-only")
    assert "getElementById" not in got.main_text


# --- the primitives -------------------------------------------------------

def test_content_hash_ignores_whitespace_but_not_words():
    assert content_hash("a  b\n c") == content_hash("a b c")
    assert content_hash("a b c") != content_hash("a b d")


def test_shingles_preserve_word_order():
    """A bag of words would call these identical. That is the whole reason for
    shingling."""
    assert shingles("dog bites man today", 4) != shingles("man bites dog today", 4)


def test_simhash_is_locality_sensitive():
    """The opposite of what a cryptographic hash promises: similar in, similar out."""
    base = " ".join(f"word{i}" for i in range(400))
    edited = base.replace("word7 ", "changed ", 1)
    assert hamming(simhash(base), simhash(base)) == 0
    assert 0 < hamming(simhash(base), simhash(edited)) <= 6
    assert hamming(simhash(base), simhash("something entirely unrelated " * 60)) > 15


def test_banding_stays_sound_for_the_configured_threshold():
    """Two fingerprints closer than BANDS bits must agree exactly on some band.
    Widening max_distance past BANDS does not make the index slower, it makes
    it WRONG — it silently stops finding the pairs it exists to find."""
    assert DuplicateIndex().max_distance < BANDS


def test_banded_lookup_finds_a_pair_at_the_measured_distance():
    index = DuplicateIndex()
    one, two = text_of("/dup/near-1"), text_of("/dup/near-2")
    assert hamming(simhash(one), simhash(two)) == 4      # measured, not assumed
    index.add("http://h/1", one)
    assert index.add("http://h/2", two).verdict is Verdict.NEAR_DUPLICATE


# --- the guards -----------------------------------------------------------

def test_short_documents_get_exact_matching_only():
    index = DuplicateIndex()
    index.add("http://h/1", "one two three four five")
    assert index.add("http://h/2", "one two three four six").verdict is Verdict.NEW


def test_degenerate_documents_are_not_fingerprinted():
    """A page can be enormous and still carry almost no distinct features. Then
    the dominant shingles have near-equal weights, cancel in every bit column,
    and noise decides the sign. Measured: this project's first generator filler
    was one phrase repeated, and two pages differing by one word in 7,679 came
    out 14 bits apart."""
    degenerate = "generated filler " * 4000
    assert len(set(shingles(degenerate))) < dedup.MIN_DISTINCT_SHINGLES
    index = DuplicateIndex()
    assert index.add("http://h/1", degenerate).verdict is Verdict.NEW
    assert index.add("http://h/2", degenerate + " tail").verdict is Verdict.NEW


def test_the_corpus_filler_is_not_degenerate():
    """The corpus had to be fixed to stop testing that degenerate case."""
    assert len(set(shingles(generated(1)))) >= dedup.MIN_DISTINCT_SHINGLES


# --- the corpus's declared relationships ----------------------------------

def test_declared_pairs_are_separable_by_a_threshold(manifest):
    """Corpus health: the declared pairs must sit below the threshold and every
    other pair above it, with room to spare. Without this margin the corpus
    cannot test near-duplicate detection at all."""
    paths = ["/a", "/b", "/c", "/slow", "/hosts", "/variants", "/",
             "/dup/exact-1", "/dup/exact-2", "/dup/near-1", "/dup/near-2"]
    fingerprints = {p: simhash(text_of(p)) for p in paths}
    declared = {frozenset(pair) for kind in ("exact", "near")
                for pair in manifest["dup_pairs"][kind]}

    threshold = DuplicateIndex().max_distance
    for a, b in combinations(paths, 2):
        distance = hamming(fingerprints[a], fingerprints[b])
        if frozenset((a, b)) in declared:
            assert distance <= threshold, f"{a}/{b} declared a pair but {distance} apart"
        else:
            assert distance > threshold * 2, f"{a}/{b} only {distance} apart"


def test_exact_pair_is_caught_by_hash_not_by_simhash(manifest):
    a, b = manifest["dup_pairs"]["exact"][0]
    assert content_hash(text_of(a)) == content_hash(text_of(b))


def test_generated_pages_are_near_duplicates_of_each_other():
    index = DuplicateIndex()
    assert index.add("http://h/gen/1", generated(1)).verdict is Verdict.NEW
    for n in (2, 3, 99):
        decision = index.add(f"http://h/gen/{n}", generated(n))
        assert decision.verdict is Verdict.NEAR_DUPLICATE
        assert decision.of == "http://h/gen/1"


def test_same_url_refetched_is_not_a_duplicate_of_itself():
    """/a, /a/ and the end of the /r/1 chain are one page reached three ways."""
    index = DuplicateIndex()
    index.add("http://h/a", text_of("/a"))
    assert index.add("http://h/a", text_of("/a")).verdict is Verdict.ALREADY_SEEN


def test_canonical_link_is_believed():
    index = DuplicateIndex()
    decision = index.add("http://h/dup/canonical-source", text_of("/dup/canonical-source"),
                         canonical="http://h/a")
    assert decision.verdict is Verdict.CANONICAL_ALIAS
    assert decision.of == "http://h/a"


# --- end to end -----------------------------------------------------------

@pytest.fixture(scope="module")
async def result(base_url=f"http://{HOST}/"):
    return await crawl(CrawlConfig(seeds=[base_url], max_depth=10, max_delay=0.0))


async def test_content_dedup_kills_the_generator_chain(result):
    """Stage 5 bounded /gen/* at the shape budget of 5. Stage 6 stops it at 3,
    on content — and the URL guard never has to fire at all."""
    gen = sorted(p for p in result.paths(HOST) if p.startswith("/gen/"))
    assert gen == ["/gen/1", "/gen/2", "/gen/3"]
    assert "shape_budget" not in result.rejected_by_traps


async def test_declared_duplicates_are_found_in_a_real_crawl(result, manifest):
    verdicts = {p.final_url.split("8081")[1]: (p.verdict, p.duplicate_of) for p in result.pages}
    exact_a, exact_b = manifest["dup_pairs"]["exact"][0]
    near_a, near_b = manifest["dup_pairs"]["near"][0]
    assert verdicts[exact_b][0] == "exact_duplicate"
    assert verdicts[exact_b][1].endswith(exact_a)
    assert verdicts[near_b][0] == "near_duplicate"
    assert verdicts[near_b][1].endswith(near_a)
    assert verdicts["/dup/canonical-source"][0] == "canonical_alias"


async def test_suppressing_duplicate_links_does_not_lose_coverage(result, manifest):
    """The risk this stage takes on: a page reachable ONLY through a duplicate
    becomes unreachable. The corpus links /dup/exact-2 from the index as well as
    from /dup/near-1 so that the risk is visible rather than accidental."""
    assert set(manifest["expected_pages"]) <= result.paths(HOST)


async def test_following_duplicate_links_lets_the_chain_run_again(base):
    """Control: with suppression off, only the stage-5 URL guard holds the trap."""
    result = await crawl(CrawlConfig(seeds=[f"{base}/"], max_depth=10, max_delay=0.0,
                                     follow_duplicate_links=True))
    gen = [p for p in result.paths(HOST) if p.startswith("/gen/")]
    assert len(gen) == 5
    assert result.rejected_by_traps.get("shape_budget", 0) > 0
