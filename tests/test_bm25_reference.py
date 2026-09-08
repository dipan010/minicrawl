"""BM25, checked against an implementation this project did not write.

The WARC was validated against `warcio` and the CDX offsets against
`warcio index`. The ranking function — the heart of stages 17 through 19 —
had only ever been checked against a brute-force scan that is in this same
repository, written by the same hand from the same reading of the formula. If
that reading were wrong, both would agree and both would be wrong.

`rank_bm25` is an independent implementation. These tests pin our scoring
against it, and pin the two places we deliberately differ so that a future
change cannot quietly become a third.

TWO DELIBERATE DIFFERENCES, AND THEY ARE PARAMETERS, NOT FORMULAS

  k1     `rank_bm25` defaults to 1.5. We use 1.2, which is Lucene's default
         and therefore what production search actually computes.

  IDF    `rank_bm25` uses the classic Robertson/Sparck-Jones formula and then
         floors negative values to `epsilon * average_idf`. We use Lucene's
         `log(1 + ...)`, which is never negative and needs no flooring.

Hold those two constant and the implementations agree to floating-point noise.
"""
import math
import random

import pytest
from rank_bm25 import BM25Okapi

from minicrawl.index import Index
from minicrawl.tokenize import tokens

VOCAB = [f"w{i}" for i in range(2000)]
WEIGHTS = [1.0 / (i + 1) for i in range(2000)]


@pytest.fixture(scope="module")
def corpus():
    """Zipfian, because a corpus where every term is common cannot tell a
    correct implementation from an incorrect one — the mistake stage 17's
    benchmark made before it was rebuilt."""
    rng = random.Random(11)
    return [" ".join(rng.choices(VOCAB, weights=WEIGHTS, k=rng.randint(30, 120)))
            for _ in range(300)]


@pytest.fixture(scope="module")
def pair(corpus):
    ours = Index()
    for text in corpus:
        ours.add(text)
    # Matched on k1 and b; the IDF variant is compared separately below.
    reference = BM25Okapi([tokens(t) for t in corpus], k1=ours.k1, b=ours.b)
    return ours, reference


def scores_with(reference, ours, query: str) -> dict[int, float]:
    """Our postings, our lengths, our saturation — the reference's IDF."""
    avg = ours.avg_length
    scores: dict[int, float] = {}
    for term in set(tokens(query)):
        for doc_id, where in ours.postings.get(term, {}).items():
            tf = len(where)
            denominator = tf + ours.k1 * (1 - ours.b + ours.b * ours.lengths[doc_id] / avg)
            scores[doc_id] = scores.get(doc_id, 0.0) + \
                reference.idf.get(term, 0.0) * tf * (ours.k1 + 1) / denominator
    return scores


def ranked(scores: dict[int, float], limit: int = 10) -> list[int]:
    return [d for d, s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            if s > 0][:limit]


# --- the corpus statistics both sides derive ------------------------------

def test_we_agree_on_average_document_length(pair):
    ours, reference = pair
    assert ours.avg_length == pytest.approx(reference.avgdl)


def test_we_agree_on_term_frequency_and_document_length(pair):
    ours, reference = pair
    for doc_id in range(0, ours.n_docs, 37):
        assert ours.lengths[doc_id] == reference.doc_len[doc_id]
        for term, count in reference.doc_freqs[doc_id].items():
            assert ours.term_frequency(term, doc_id) == count


# --- the scoring function itself -------------------------------------------

def test_our_scoring_matches_the_reference_exactly(pair):
    """The headline. Same k1, same b, same IDF numbers: any difference left is
    ours, and there is none beyond floating-point noise."""
    ours, reference = pair
    rng = random.Random(3)
    worst = 0.0
    for _ in range(120):
        query = " ".join(rng.sample(VOCAB[:600], k=rng.randint(1, 3)))
        theirs = reference.get_scores(tokens(query))
        for doc_id, score in scores_with(reference, ours, query).items():
            worst = max(worst, abs(score - theirs[doc_id]))
    assert worst < 1e-9, f"largest divergence {worst:.3e}"


def test_full_rankings_match_the_reference(pair):
    ours, reference = pair
    rng = random.Random(5)
    compared = 0
    for _ in range(120):
        query = " ".join(rng.sample(VOCAB[:600], k=rng.randint(1, 3)))
        theirs = ranked({i: s for i, s in enumerate(reference.get_scores(tokens(query)))})
        if not theirs:
            continue
        compared += 1
        assert ranked(scores_with(reference, ours, query)) == theirs, query
    assert compared > 50, "not enough queries matched anything to be meaningful"


def test_single_term_rankings_match_without_any_idf_adjustment(pair):
    """For one term, IDF is a single constant multiplying every score, so it
    cannot change the order. This compares OUR ranking, unmodified, against
    the reference — and isolates saturation and length normalisation, which
    are the only things left that could differ."""
    ours, reference = pair
    checked = 0
    for term in VOCAB[:400]:
        if not ours.doc_frequency(term):
            continue
        checked += 1
        theirs = ranked({i: s for i, s in enumerate(reference.get_scores([term]))})
        assert [h.doc_id for h in ours.search(term, limit=10)] == theirs, term
    assert checked > 100


# --- the two deliberate differences ---------------------------------------

def test_our_idf_is_lucene_s_not_the_classic_one(pair):
    """Pinned because it is a real departure from the textbook, made on
    purpose: the classic formula goes negative for a term in more than half
    the corpus, which lets a document rank lower for containing a word you
    searched for."""
    ours, _ = pair
    n = ours.n_docs
    for term in ["w0", "w1", "w50", "w500"]:
        df = ours.doc_frequency(term)
        if not df:
            continue
        lucene = math.log(1 + (n - df + 0.5) / (df + 0.5))
        assert ours.idf(term) == pytest.approx(lucene, abs=1e-12)


def test_the_classic_idf_would_go_negative_where_ours_does_not(pair):
    ours, _ = pair
    n = ours.n_docs
    common = max(VOCAB[:50], key=ours.doc_frequency)
    df = ours.doc_frequency(common)
    assert df > n / 2, "need a term in more than half the corpus"
    assert math.log((n - df + 0.5) / (df + 0.5)) < 0     # the textbook formula
    assert ours.idf(common) > 0                          # ours


def test_the_k1_plus_1_numerator_never_changes_an_ordering(pair):
    """Lucene omits the (k1+1) factor we keep. It is a uniform multiplier, so
    scores differ by exactly k1+1 and rankings do not differ at all."""
    ours, _ = pair
    rng = random.Random(7)
    for _ in range(40):
        query = " ".join(rng.sample(VOCAB[:600], k=rng.randint(1, 3)))
        hits = ours.search(query, limit=10)
        if len(hits) < 2:
            continue
        rescaled = sorted(((h.score / (ours.k1 + 1), h.doc_id) for h in hits),
                          key=lambda kv: (-kv[0], kv[1]))
        assert [d for _, d in rescaled] == [h.doc_id for h in hits]


def test_the_reference_default_k1_differs_from_ours(pair):
    """The whole reason an early comparison looked like a formula bug. Left as
    a test so a future version bump that changes the default is noticed."""
    ours, _ = pair
    assert ours.k1 == 1.2, "Lucene's default"
    assert BM25Okapi([["a"]]).k1 == 1.5, "rank_bm25's default"
