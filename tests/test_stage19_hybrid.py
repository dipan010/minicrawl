"""Stage 19 — a second retriever, and fusing two that disagree.

The claims: n-grams match across word endings where BM25 cannot, they are
honestly bad at meaning, and Reciprocal Rank Fusion combines the two without
needing either score to be comparable with the other.
"""
import pytest

from minicrawl.hybrid import HybridSearcher, reciprocal_rank_fusion
from minicrawl.index import Index
from minicrawl.vectors import NgramIndex, cosine, grams, norm, vector


# --- n-grams ---------------------------------------------------------------

def test_boundary_markers_are_part_of_the_gram():
    """Without ^ and $, a prefix stops counting for anything: `redirect` would
    match `predirect` as readily as `redirects`."""
    assert grams("cat") == ["^ca", "cat", "at$"]
    assert "^cr" in grams("crawler") and "er$" in grams("crawler")


def test_a_word_shorter_than_the_gram_size_still_produces_one():
    assert grams("a") == ["^a$"]


def test_a_word_and_its_plural_are_mostly_the_same_vector():
    """The failure this stage exists for: BM25 sees two unrelated terms."""
    assert cosine(vector("redirect"), vector("redirects")) > 0.8


def test_unrelated_words_share_almost_nothing():
    assert cosine(vector("redirect"), vector("crawler")) < 0.1


def test_ngrams_do_not_know_what_words_mean():
    """Stated as a test so nobody reads this file as semantic search.
    `crawler` and `spider` are the same idea and share no letters worth
    speaking of; a dense embedding would know, and this cannot."""
    assert cosine(vector("crawler"), vector("spider")) < 0.3


def test_cosine_is_bounded_and_symmetric():
    a, b = vector("crawler politeness"), vector("polite crawling")
    assert 0.0 <= cosine(a, b) <= 1.0
    assert cosine(a, b) == pytest.approx(cosine(b, a))
    assert cosine(a, a) == pytest.approx(1.0)


def test_cosine_ignores_length():
    """A long page and a short one about the same thing must score alike —
    the problem BM25 solves with length normalisation, solved differently."""
    short, long = vector("crawler"), vector("crawler " * 50)
    assert cosine(short, long) == pytest.approx(1.0)


def test_an_empty_vector_scores_zero_rather_than_dividing_by_zero():
    assert cosine({}, vector("crawler")) == 0.0
    assert norm({}) == 0.0


def test_only_documents_sharing_a_gram_are_scored():
    """The naive version compares the query with every document, which is the
    forward scan stage 17 spent a stage avoiding."""
    index = NgramIndex()
    index.add("crawler politeness")
    index.add("zzzz qqqq")
    hits = index.search("crawling")
    assert [h.doc_id for h in hits] == [0]


def test_the_ngram_index_finds_a_word_form_it_never_saw():
    index = NgramIndex()
    index.add("Redirects mean the URL you asked for is not the one you got",
              url="u1")
    index.add("Politeness means waiting between requests", url="u2")
    assert [h.url for h in index.search("redirect", limit=1)] == ["u1"]


# --- fusion ----------------------------------------------------------------

def test_fusion_uses_ranks_not_scores():
    """The whole argument for RRF: a BM25 score is unbounded and a cosine is
    in [0,1], so adding them means guessing a scale. Only order is comparable,
    and a document ranked first contributes the same from either retriever."""
    one = reciprocal_rank_fusion({"a": [7], "b": []})
    two = reciprocal_rank_fusion({"a": [], "b": [7]})
    assert one[0].score == two[0].score


def test_a_document_both_retrievers_like_beats_one_only_one_likes():
    fused = reciprocal_rank_fusion({"a": [1, 2], "b": [2, 3]}, limit=10)
    assert fused[0].doc_id == 2


def test_a_retriever_that_returns_nothing_contributes_nothing():
    with_empty = reciprocal_rank_fusion({"a": [4, 5], "b": []})
    alone = reciprocal_rank_fusion({"a": [4, 5]})
    assert [h.doc_id for h in with_empty] == [h.doc_id for h in alone]
    assert with_empty[0].score == pytest.approx(alone[0].score)


def test_rank_one_from_two_retrievers_beats_rank_one_from_one():
    fused = reciprocal_rank_fusion({"a": [1], "b": [1]})
    solo = reciprocal_rank_fusion({"a": [2]})
    assert fused[0].score > solo[0].score


def test_k_controls_how_sharply_rank_matters():
    """A small k lets one retriever's top hit dominate, which is single
    retrieval wearing a hat."""
    sharp = reciprocal_rank_fusion({"a": [1, 2]}, k=1)
    flat = reciprocal_rank_fusion({"a": [1, 2]}, k=60)
    assert sharp[0].score / sharp[1].score > flat[0].score / flat[1].score


def test_the_fused_hit_says_where_it_came_from():
    fused = reciprocal_rank_fusion({"bm25": [3], "ngram": [9, 3]})
    top = next(h for h in fused if h.doc_id == 3)
    assert top.ranks == {"bm25": 1, "ngram": 2}
    assert "bm25 #1" in top.explain()


def test_fusion_is_deterministic_for_tied_scores():
    a = reciprocal_rank_fusion({"x": [5, 6], "y": [6, 5]}, limit=2)
    b = reciprocal_rank_fusion({"x": [5, 6], "y": [6, 5]}, limit=2)
    assert [h.doc_id for h in a] == [h.doc_id for h in b]


# --- the two together ------------------------------------------------------

def hybrid_from(*docs) -> HybridSearcher:
    index, ngrams = Index(), NgramIndex()
    for i, text in enumerate(docs):
        index.add(text, url=f"http://h/{i}")
        ngrams.add(text, url=f"http://h/{i}")
    return HybridSearcher(index, ngrams)


def test_hybrid_answers_a_query_bm25_cannot():
    searcher = hybrid_from("Redirects change where you land",
                           "Politeness means waiting")
    assert searcher.index.search("redirect") == [], "BM25 must miss this"
    assert [h.doc_id for h in searcher.search("redirect")][:1] == [0]


def test_hybrid_keeps_what_bm25_already_answered():
    """A fusion that improves recall by wrecking precision is not an
    improvement."""
    searcher = hybrid_from("crawler politeness and delays",
                           "unrelated text about weather",
                           "crawler")
    best = searcher.index.search("crawler politeness")[0].doc_id
    assert best in [h.doc_id for h in searcher.search("crawler politeness")]


def test_compare_shows_what_each_retriever_alone_would_have_missed():
    searcher = hybrid_from("Redirects change where you land",
                           "Politeness means waiting")
    comparison = searcher.compare("redirect")
    assert comparison["bm25"] == []
    assert comparison["only_ngram"]
    assert comparison["fused"]


def test_the_two_retrievers_agree_on_what_a_document_id_means():
    """Fusion is by id. If the retrievers ever drifted out of step, every
    result would be subtly wrong and nothing would look broken."""
    index, ngrams = Index(), NgramIndex()
    for i in range(5):
        assert index.add(f"text {i}", url=f"http://h/{i}") == \
               ngrams.add(f"text {i}", url=f"http://h/{i}")


async def test_building_both_retrievers_from_one_export(tmp_path, base):
    from minicrawl.crawler import CrawlConfig, crawl
    from minicrawl.export import JsonlExporter

    exporter = JsonlExporter(tmp_path / "pages.jsonl")
    await crawl(CrawlConfig(seeds=[base + "/"], max_pages=40, max_depth=3,
                            exporter=exporter, on_page=None))
    searcher = HybridSearcher.build(tmp_path / "pages.jsonl")

    assert searcher.index.n_docs == len(searcher.ngrams.vectors)
    # The headline: a word the corpus contains, in a form it does not.
    assert searcher.index.search("redirect") == []
    assert searcher.index.search("redirects"), "the corpus does contain it"
    assert searcher.search("redirect"), "fusion must reach it"


def test_a_query_matching_nothing_anywhere_returns_nothing():
    searcher = hybrid_from("crawler", "politeness")
    assert searcher.search("zzzzqqqq") == []
