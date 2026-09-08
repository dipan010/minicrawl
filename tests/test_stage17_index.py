"""Stage 17 — the inverted index and BM25.

The claims: the tokeniser decides what is findable, BM25 behaves the way BM25
is supposed to, and the index returns exactly what a brute-force scan returns
— because a faster structure that gives different answers is not faster, it is
wrong.
"""
import json
import math

import pytest

from minicrawl.index import Index
from minicrawl.tokenize import MAX_TERM_CHARS, counted, normalize, tokens


# --- the tokeniser decides what can ever be found --------------------------

def test_case_is_folded_so_one_word_is_one_term():
    assert tokens("Crawler CRAWLER crawler") == ["crawler"] * 3


def test_digits_survive_because_people_search_for_them():
    assert tokens("RFC 9309 and HTTP/2") == ["rfc", "9309", "and", "http", "2"]


def test_non_ascii_words_are_kept_whole():
    """Splitting on ASCII word characters would silently drop most of the web."""
    assert tokens("naïve café 日本語 Ünicode") == ["naïve", "café", "日本語", "ünicode"]


def test_a_ligature_matches_what_a_person_would_type():
    """Without NFKC, `ﬁle` and `file` are different terms and a document using
    typographic ligatures is unsearchable."""
    assert tokens("ﬁle") == tokens("file") == ["file"]


def test_an_apostrophe_does_not_split_a_word():
    assert tokens("don't") == ["don't"]
    assert tokens("it’s") == ["it’s"]


def test_absurdly_long_tokens_are_dropped():
    """A 'word' of 500 characters is a hash or minified markup, and indexing it
    costs a postings entry that no query will ever match."""
    assert tokens("x" * (MAX_TERM_CHARS + 1)) == []
    assert tokens("x" * MAX_TERM_CHARS) == ["x" * MAX_TERM_CHARS]


def test_counted_returns_term_frequencies():
    assert counted("the cat the hat the") == {"the": 3, "cat": 1, "hat": 1}


def test_normalize_is_idempotent():
    once = normalize("Ünicode ﬁle")
    assert normalize(once) == once


# --- BM25 behaves like BM25 ------------------------------------------------

def test_term_frequency_saturates():
    """The tenth mention must add almost nothing over the ninth. A document
    mentioning a word 50 times is not 50x more relevant, and a scoring function
    that says so is just counting."""
    index = Index()
    index.add("crawler " * 1 + "filler " * 49)
    index.add("crawler " * 50)
    hits = {h.doc_id: h.score for h in index.search("crawler")}
    ratio = hits[1] / hits[0]
    assert 1 < ratio < 3, f"50x the mentions gave {ratio:.1f}x the score"


def test_a_shorter_document_wins_on_the_same_term_count():
    """Length normalisation: a long document contains more of everything, so
    matching in it is less impressive."""
    index = Index()
    index.add("crawler " + "padding " * 200)
    index.add("crawler")
    hits = index.search("crawler")
    assert hits[0].doc_id == 1


def test_idf_is_never_negative():
    """The classic formula goes negative for a term in more than half the
    corpus, which lets a document rank LOWER for containing a word you searched
    for. The +1 variant removes that."""
    index = Index()
    for _ in range(10):
        index.add("common word here")
    index.add("common word here rare")
    assert index.idf("common") >= 0
    assert index.idf("rare") > index.idf("common")


def test_a_term_in_every_document_barely_contributes():
    """This is why no stopword list is needed: BM25 already discounts them."""
    index = Index()
    for i in range(50):
        index.add(f"the document number {i}")
    assert index.idf("the") < 0.1


def test_a_rarer_term_outranks_a_common_one():
    index = Index()
    for _ in range(30):
        index.add("politeness matters")
    index.add("politeness simhash")
    hits = index.search("politeness simhash")
    assert hits[0].doc_id == 30
    assert hits[0].matched["simhash"] > hits[0].matched["politeness"]


def test_a_title_counts_for_more_than_a_passing_mention():
    index = Index()
    index.add("a paragraph that mentions crawlers once", title="Unrelated")
    index.add("nothing much here at all", title="Crawlers")
    assert index.search("crawlers")[0].doc_id == 1


# --- the structure must not change the answer ------------------------------

def brute_force(index: Index, query: str, limit: int = 10):
    """The same BM25, computed by looking at every document."""
    terms = set(tokens(query))
    avg = index.avg_length or 1.0
    scored = []
    for doc_id in range(index.n_docs):
        score = 0.0
        for term in terms:
            frequency = index.postings.get(term, {}).get(doc_id, 0)
            if not frequency:
                continue
            ratio = index.lengths[doc_id] / avg
            score += index.idf(term) * (frequency * (index.k1 + 1)) / (
                frequency + index.k1 * (1 - index.b + index.b * ratio))
        if score:
            scored.append((doc_id, score))
    return sorted(scored, key=lambda kv: (-kv[1], kv[0]))[:limit]


def test_the_index_returns_exactly_what_a_full_scan_returns():
    """A faster structure that gives different answers is not faster."""
    import random
    rng = random.Random(11)
    words = ["crawler", "robots", "index", "duplicate", "url", "page", "text",
             "queue", "host", "delay", "simhash", "archive"]
    index = Index()
    for _ in range(300):
        index.add(" ".join(rng.choices(words, k=rng.randint(5, 40))))

    for query in ["crawler", "robots url", "simhash duplicate archive",
                  "queue", "nothing matches this"]:
        fast = [(h.doc_id, round(h.score, 9)) for h in index.search(query)]
        slow = [(d, round(s, 9)) for d, s in brute_force(index, query)]
        assert fast == slow, query


def test_documents_containing_no_query_term_are_never_scored():
    index = Index()
    index.add("alpha")
    index.add("beta")
    index.add("gamma")
    assert [h.doc_id for h in index.search("beta")] == [1]


# --- edges -----------------------------------------------------------------

@pytest.mark.parametrize("query", ["", "   ", "!!!", "___"])
def test_an_empty_query_returns_nothing_rather_than_everything(query):
    index = Index()
    index.add("some text")
    assert index.search(query) == []


def test_searching_an_empty_index_is_not_an_error():
    assert Index().search("anything") == []


def test_a_term_nobody_indexed_scores_nothing():
    index = Index()
    index.add("present")
    assert index.idf("absent") == 0.0
    assert index.search("absent") == []


# --- persistence -----------------------------------------------------------

def test_a_saved_index_loads_back_identical(tmp_path):
    index = Index()
    for i, text in enumerate(["crawler robots", "index duplicate", "url page"]):
        index.add(text, url=f"http://h/{i}", title=f"T{i}")
    before = [(h.doc_id, round(h.score, 9), h.url) for h in index.search("crawler url")]

    index.save(tmp_path / "i.json")
    loaded = Index.load(tmp_path / "i.json")

    assert loaded.stats() == index.stats()
    after = [(h.doc_id, round(h.score, 9), h.url) for h in loaded.search("crawler url")]
    assert after == before


# --- reading a stage-15 export --------------------------------------------

def test_indexing_an_export_skips_records_with_no_text(tmp_path):
    """A 304 and a PDF are real crawled pages with no text. They must not
    become empty documents — an empty document still changes the average
    length, which changes every score in the index."""
    path = tmp_path / "pages.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"final_url": "http://h/a", "title": "A", "text": "crawler politeness"},
        {"final_url": "http://h/b", "title": "B", "text": None},
        {"final_url": "http://h/c", "title": "C", "text": ""},
        {"final_url": "http://h/d", "title": "D", "text": "robots exclusion"},
    ]) + "\n", encoding="utf-8")

    index = Index()
    assert index.add_jsonl(path) == 2
    assert index.n_docs == 2
    assert [h.url for h in index.search("robots")] == ["http://h/d"]


def test_indexing_an_export_keeps_the_url_and_title(tmp_path):
    path = tmp_path / "pages.jsonl"
    path.write_text(json.dumps(
        {"final_url": "http://h/a", "title": "Page A", "text": "simhash"}) + "\n",
        encoding="utf-8")
    index = Index()
    index.add_jsonl(path)
    hit = index.search("simhash")[0]
    assert hit.url == "http://h/a" and hit.title == "Page A"


# --- through a real crawl --------------------------------------------------

async def test_a_crawl_export_becomes_a_searchable_index(tmp_path, base):
    """End to end: the seam between stage 15 and stage 17.

    The non-UTF-8 pages are the interesting assertion — a query for `café`
    only finds them if stage 13's decoding survived the export and the
    tokeniser's normalisation.
    """
    from minicrawl.crawler import CrawlConfig, crawl
    from minicrawl.export import JsonlExporter

    exporter = JsonlExporter(tmp_path / "pages.jsonl")
    await crawl(CrawlConfig(seeds=[base + "/"], max_pages=40, max_depth=3,
                            exporter=exporter, on_page=None))

    index = Index()
    assert index.add_jsonl(tmp_path / "pages.jsonl") > 10

    hits = index.search("café")
    assert hits, "the accented pages should be findable"
    assert all("/encoded/" in h.url for h in hits), [h.url for h in hits]


def test_the_explanation_adds_up_to_the_score():
    """A score nobody can decompose is a number you have to trust."""
    index = Index()
    index.add("crawler robots politeness")
    index.add("crawler only")
    hit = index.search("crawler robots")[0]
    assert math.isclose(sum(hit.matched.values()), hit.score, rel_tol=1e-9)
    assert "crawler" in hit.explain()
