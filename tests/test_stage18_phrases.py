"""Stage 18 — positions, and the phrase queries they buy.

A posting now records WHERE a term occurs, not just how often. The claims:
adjacency is real adjacency, a field boundary is not a place phrases may
cross, and none of it changed how a plain term query scores.
"""
import json

import pytest

from minicrawl.index import FIELD_GAP, Index, parse_query


def build(*docs, titles=None) -> Index:
    index = Index()
    for i, text in enumerate(docs):
        index.add(text, url=f"http://h/{i}",
                  title=(titles[i] if titles else ""))
    return index


# --- the parser ------------------------------------------------------------

def test_quotes_separate_phrases_from_loose_terms():
    parsed = parse_query('web "quick brown" fox')
    assert parsed.phrases == [["quick", "brown"]]
    assert parsed.terms == ["web", "fox"]


def test_several_phrases_are_all_kept():
    parsed = parse_query('"one two" and "three four"')
    assert parsed.phrases == [["one", "two"], ["three", "four"]]
    assert parsed.terms == ["and"]


def test_a_quoted_single_word_is_just_that_word():
    parsed = parse_query('"crawler"')
    assert parsed.phrases == [] and parsed.terms == ["crawler"]


def test_an_unterminated_quote_is_treated_as_text_not_an_error():
    """Somebody who mistypes a quote wants results, not a syntax lecture."""
    parsed = parse_query('web "crawler politeness')
    assert parsed.phrases == []
    assert set(parsed.terms) == {"web", "crawler", "politeness"}


def test_an_empty_quote_is_ignored():
    assert parse_query('"" crawler').terms == ["crawler"]


# --- adjacency is real adjacency -------------------------------------------

def test_a_phrase_requires_the_words_next_to_each_other():
    index = build("the quick brown fox",
                  "brown things are quick, unlike this ordering")
    assert [h.doc_id for h in index.search('"quick brown"')] == [0]


def test_a_phrase_requires_the_given_order():
    index = build("the quick brown fox")
    assert index.search('"quick brown"')
    assert index.search('"brown quick"') == []


def test_a_three_word_phrase_needs_all_three_adjacent():
    index = build("robots exclusion protocol",
                  "robots exclusion and, separately, protocol")
    assert [h.doc_id for h in index.search('"robots exclusion protocol"')] == [0]


def test_a_phrase_can_match_more_than_once_in_a_document():
    """Both documents contain the phrase; the one containing it twice must
    score higher, because the phrase filter does not replace scoring."""
    index = build("a quick brown fox and another quick brown fox",
                  "one quick brown fox only")
    hits = index.search('"quick brown"')
    assert [h.doc_id for h in hits] == [0, 1]


def test_several_phrases_must_all_be_present():
    index = build("robots exclusion and duplicate detection",
                  "robots exclusion only")
    assert [h.doc_id for h in index.search('"robots exclusion" "duplicate detection"')] == [0]


def test_a_phrase_that_appears_nowhere_returns_nothing_not_everything():
    index = build("robots exclusion protocol", "politeness and delays")
    assert index.search('"protocol politeness"') == []


def test_loose_terms_match_more_than_the_same_words_quoted():
    index = build("quick brown", "quick things and brown things")
    assert len(index.search("quick brown")) == 2
    assert len(index.search('"quick brown"')) == 1


def test_a_phrase_filters_while_a_loose_term_only_ranks():
    """Both documents contain the phrase, so both survive the filter. The
    loose term decides the order rather than the membership — quoting one part
    of a query must not silently drop results matching the rest."""
    index = build("quick brown fox", "quick brown badger")
    hits = index.search('"quick brown" fox')
    assert [h.doc_id for h in hits] == [0, 1]
    assert hits[0].score > hits[1].score


# --- field boundaries ------------------------------------------------------

def test_a_phrase_cannot_straddle_the_title_and_the_body():
    """A title ending 'Crawler' followed by a body starting 'Politeness'
    contains neither the phrase 'crawler politeness' nor anything like it."""
    index = build("Politeness means waiting", titles=["Web Crawler"])
    assert index.search('"crawler politeness"') == []
    assert index.search('"web crawler"'), "the real title phrase must still match"


def test_weighting_the_title_does_not_invent_a_phrase():
    """The title is indexed twice for weight. Laid end to end, 'Web Crawler'
    would become web crawler web crawler and manufacture 'crawler web' — a
    phrase the document does not contain. The copies are separate fields."""
    index = build("body text", titles=["Web Crawler"])
    assert index.search('"crawler web"') == []


def test_the_gap_is_wider_than_any_phrase_anyone_will_type():
    assert FIELD_GAP > 100


def test_document_length_counts_terms_not_the_padded_span():
    """The gap is a positional device. If it leaked into length, every
    document would look longer than it is and every BM25 score would shift."""
    index = build("one two three", titles=["A"])
    assert index.lengths[0] == 2 * 1 + 3


# --- nothing about term scoring changed ------------------------------------

def test_term_frequency_is_derived_from_positions():
    index = build("crawler crawler crawler")
    assert index.term_frequency("crawler", 0) == 3
    assert index.doc_frequency("crawler") == 1


def test_a_plain_query_still_ranks_on_frequency_when_length_is_equal():
    """Positions buy phrases without moving any existing ranking. Length is
    held equal here so the comparison is about frequency alone."""
    index = build("crawler robots padding padding",
                  "crawler crawler robots padding")
    assert index.lengths[0] == index.lengths[1]
    hits = index.search("crawler robots")
    assert [h.doc_id for h in hits] == [1, 0]
    assert abs(sum(hits[0].matched.values()) - hits[0].score) < 1e-9


def test_a_shorter_document_can_beat_more_occurrences():
    """BM25 is not a counter. A two-word document mentioning `crawler` once
    outranks a four-word one mentioning it twice, because saturation caps what
    the second mention adds while length normalisation keeps discounting."""
    index = build("crawler robots", "crawler crawler robots url")
    hits = index.search("crawler robots")
    assert [h.doc_id for h in hits] == [0, 1]
    # The longer document does score higher on `crawler` itself — it simply
    # loses more on `robots` than it gains.
    assert hits[1].matched["crawler"] > hits[0].matched["crawler"]


def test_stats_report_positions_separately_from_postings():
    index = build("a a a b")
    stats = index.stats()
    assert stats["postings"] == 2 and stats["positions"] == 4


# --- persistence -----------------------------------------------------------

def test_positions_survive_a_save_and_load(tmp_path):
    index = build("the quick brown fox jumps")
    index.save(tmp_path / "i.json")
    loaded = Index.load(tmp_path / "i.json")
    assert loaded.postings == index.postings
    assert [h.doc_id for h in loaded.search('"quick brown"')] == [0]


def test_an_index_without_positions_is_refused_rather_than_mis_answered(tmp_path):
    """A v1 index stored counts. Loading it would answer term queries
    correctly and every phrase query wrongly — the worst kind of
    compatibility, so it is refused with an instruction instead."""
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "version": 1, "k1": 1.2, "b": 0.75,
        "docs": [{"url": "http://h/0", "title": ""}], "lengths": [2],
        "postings": {"crawler": [[0, 1]], "robots": [[0, 1]]},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="rebuild"):
        Index.load(path)


# --- against a real crawl --------------------------------------------------

async def test_phrase_search_over_a_crawled_corpus(tmp_path, base):
    from minicrawl.crawler import CrawlConfig, crawl
    from minicrawl.export import JsonlExporter

    exporter = JsonlExporter(tmp_path / "pages.jsonl")
    await crawl(CrawlConfig(seeds=[base + "/"], max_pages=40, max_depth=3,
                            exporter=exporter, on_page=None))
    index = Index()
    index.add_jsonl(tmp_path / "pages.jsonl")

    phrase = index.search('"the url you asked"')
    assert phrase, "that phrase is in the corpus text"
    assert index.search('"you asked the url"') == [], "reversed, it is not"
    # Every phrase hit must also be a hit for the loose query.
    loose = {h.doc_id for h in index.search("the url you asked", limit=1000)}
    assert {h.doc_id for h in phrase} <= loose
