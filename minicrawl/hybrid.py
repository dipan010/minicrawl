"""Stage 19b — combining two retrievers that disagree.

BM25 and n-gram similarity fail in opposite directions. BM25 is precise and
brittle: it will not match `redirect` against `Redirects`. N-grams are
forgiving and noisy: they will match `crawler` against `spider` a little,
because both contain `er`, which means nothing at all.

Neither is better. Used alone, each has a characteristic way of being wrong,
and the useful system is the one that asks both.

WHY RANKS, NOT SCORES

The obvious combination is `a * bm25 + b * cosine`. It does not work, and the
reason is worth understanding: **the two numbers are not comparable**. A BM25
score is unbounded, corpus-dependent and grows with query length; a cosine is
in [0, 1] by construction. Adding them means picking weights that are really
just a guess about scale, and the guess has to be retuned whenever the corpus
changes.

Reciprocal Rank Fusion throws the scores away and keeps only the ORDER:

    score(d) = sum over retrievers of  1 / (k + rank(d))

Rank is comparable across anything. A document ranked 1st by either retriever
contributes the same amount regardless of whether that retriever reports 14.2
or 0.83. RRF needs no tuning, no normalisation and no knowledge of either
scoring function, and it beats most carefully weighted combinations in the
published comparisons — which is either humbling or liberating.

`k` (60 by convention, from Cormack et al. 2009) sets how sharply rank matters.
It is deliberately large: with k=60 the difference between rank 1 and rank 2 is
small, so a retriever must be confident ACROSS several positions to dominate.
A small k would let one retriever's top hit win every time, which is single
retrieval wearing a hat.
"""
from __future__ import annotations

from dataclasses import dataclass, field

RRF_K = 60


@dataclass(slots=True)
class FusedHit:
    doc_id: int
    score: float
    url: str = ""
    title: str = ""
    ranks: dict[str, int] = field(default_factory=dict)

    def explain(self) -> str:
        if not self.ranks:
            return f"{self.score:.4f}"
        parts = ", ".join(f"{name} #{rank}" for name, rank in sorted(self.ranks.items()))
        return f"{self.score:.4f}  ({parts})"


def reciprocal_rank_fusion(rankings: dict[str, list[int]],
                           k: int = RRF_K, limit: int = 10) -> list[FusedHit]:
    """Fuse several ranked lists of document ids.

    Only the position of a document matters, so a retriever that returns
    nothing simply contributes nothing — no special case, no renormalising.
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, ordered in rankings.items():
        for position, doc_id in enumerate(ordered, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position)
            ranks.setdefault(doc_id, {})[name] = position

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [FusedHit(doc_id=doc_id, score=score, ranks=ranks[doc_id])
            for doc_id, score in ordered]


class HybridSearcher:
    """One query, two retrievers, one ranked answer."""

    def __init__(self, index, ngrams, k: int = RRF_K, depth: int = 50):
        self.index = index          # minicrawl.index.Index — BM25
        self.ngrams = ngrams        # minicrawl.vectors.NgramIndex
        self.k = k
        # How deep each retriever is asked before fusing. Too shallow and a
        # document neither ranks highly can never surface, which is exactly
        # the case hybrid exists to rescue.
        self.depth = depth

    @classmethod
    def build(cls, jsonl_path, **kwargs) -> "HybridSearcher":
        """Build both retrievers from one stage-15 export, in one pass."""
        import json
        from pathlib import Path

        from .index import Index
        from .vectors import NgramIndex

        index, ngrams = Index(), NgramIndex()
        with Path(jsonl_path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                text = record.get("text")
                if not text:
                    continue
                url = record.get("final_url") or record.get("url", "")
                title = record.get("title", "")
                # Added to both in the same order, so a document id means the
                # same thing in each. Fusion is by id; if the two ever
                # disagreed about what document 7 is, every result would be
                # subtly wrong and nothing would look broken.
                a = index.add(text, url=url, title=title)
                b = ngrams.add(text, url=url, title=title)
                assert a == b, "retrievers drifted out of step"
        return cls(index, ngrams, **kwargs)

    def search(self, query: str, limit: int = 10) -> list[FusedHit]:
        bm25 = [h.doc_id for h in self.index.search(query, limit=self.depth)]
        vectors = [h.doc_id for h in self.ngrams.search(query, limit=self.depth)]
        fused = reciprocal_rank_fusion({"bm25": bm25, "ngram": vectors},
                                       k=self.k, limit=limit)
        for hit in fused:
            meta = self.index.docs[hit.doc_id]
            hit.url, hit.title = meta.get("url", ""), meta.get("title", "")
        return fused

    def compare(self, query: str, limit: int = 10) -> dict:
        """What each retriever found, and what only the other one did.

        The point of the stage in one method: if the two lists agree, hybrid
        bought nothing for this query, and saying so is more useful than a
        combined number that hides it.
        """
        bm25 = [h.doc_id for h in self.index.search(query, limit=limit)]
        vectors = [h.doc_id for h in self.ngrams.search(query, limit=limit)]
        return {"bm25": bm25, "ngram": vectors,
                "only_bm25": [d for d in bm25 if d not in vectors],
                "only_ngram": [d for d in vectors if d not in bm25],
                "both": [d for d in bm25 if d in vectors],
                "fused": [h.doc_id for h in self.search(query, limit=limit)]}
