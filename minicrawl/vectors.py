"""Stage 19a — a second opinion: character n-grams.

BM25 matches terms. If the query says `redirect` and the document says
`Redirects`, those are two different terms and the document is invisible —
which is not a subtle failure, it is the most common complaint anyone has
about a keyword index. Stage 17 named it as a gap and left it there.

WHAT THIS IS, AND WHAT IT IS NOT

A character n-gram vector is a document represented by the little sequences of
letters it contains. `crawler` becomes:

    ^cr  cra  raw  awl  wle  ler  er$

`crawlers` shares six of those seven. `redirect` and `redirects` share all but
one. So a similarity computed over n-grams matches across word endings,
compounds and typos, with no stemmer, no rule table and no language
assumption — which matters, because a stemmer written for English does the
wrong thing to every other language in a crawl.

It is emphatically NOT a semantic embedding. It has no idea that `crawler` and
`spider` mean the same thing, because they share no letters. A dense embedding
from a trained model would know that, and would cost a model: `torch` alone is
larger than every dependency this project has combined, against a core of two.
That trade is stated in `docs/stage-19.md` rather than pretended away — this
file fixes MORPHOLOGY, and calling it semantic would be a lie that sounds
better.

WHY COSINE

Two documents about the same thing at different lengths have proportional
n-gram counts, not equal ones. Cosine compares direction and ignores
magnitude, so a long page and a short one about the same subject score alike —
the same problem BM25 solves with length normalisation, solved differently.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .tokenize import normalize, tokens

N = 3                    # trigrams: short enough to survive short words
MIN_GRAM_DOCS = 1


def grams(word: str, n: int = N) -> list[str]:
    """Character n-grams of one word, with boundary markers.

    The `^` and `$` matter more than they look. Without them `ler` in
    `crawler` and `ler` in `lerner` are indistinguishable, and prefixes stop
    counting for anything — `redirect` would match `predirect` as readily as
    `redirects`.
    """
    padded = f"^{word}$"
    if len(padded) <= n:
        return [padded]
    return [padded[i:i + n] for i in range(len(padded) - n + 1)]


def vector(text: str, n: int = N) -> dict[str, int]:
    """The n-gram counts of a whole text."""
    counts: dict[str, int] = {}
    for word in tokens(text):
        for gram in grams(word, n):
            counts[gram] = counts.get(gram, 0) + 1
    return counts


def norm(counts: dict[str, int]) -> float:
    return math.sqrt(sum(v * v for v in counts.values()))


def cosine(a: dict[str, int], b: dict[str, int],
           a_norm: float | None = None, b_norm: float | None = None) -> float:
    """Cosine similarity of two sparse count vectors, in [0, 1].

    Iterates the SHORTER vector: the query is almost always tiny compared with
    a document, and walking the document's thousands of grams to look each one
    up in the query's dozen is the same answer computed the expensive way.
    """
    if not a or not b:
        return 0.0
    if len(b) < len(a):
        a, b = b, a
        a_norm, b_norm = b_norm, a_norm
    shared = sum(count * b.get(gram, 0) for gram, count in a.items())
    if not shared:
        return 0.0
    denominator = (a_norm or norm(a)) * (b_norm or norm(b))
    return shared / denominator if denominator else 0.0


@dataclass(slots=True)
class VectorHit:
    doc_id: int
    score: float
    url: str = ""
    title: str = ""


class NgramIndex:
    """Documents as n-gram vectors, searchable without scanning them all.

    The naive version compares the query against every document, which is the
    forward scan stage 17 spent a whole stage avoiding. So n-grams get their
    own postings list, and only documents sharing at least one gram with the
    query are ever scored.
    """

    def __init__(self, n: int = N):
        self.n = n
        self.vectors: list[dict[str, int]] = []
        self.norms: list[float] = []
        self.docs: list[dict] = []
        self.postings: dict[str, set[int]] = {}

    def add(self, text: str, *, url: str = "", title: str = "") -> int:
        doc_id = len(self.vectors)
        counts = vector(f"{title} {text}" if title else text, self.n)
        self.vectors.append(counts)
        self.norms.append(norm(counts))
        self.docs.append({"url": url, "title": title})
        for gram in counts:
            self.postings.setdefault(gram, set()).add(doc_id)
        return doc_id

    def search(self, query: str, limit: int = 10,
               min_score: float = 0.0) -> list[VectorHit]:
        counts = vector(query, self.n)
        if not counts:
            return []
        query_norm = norm(counts)

        candidates: set[int] = set()
        for gram in counts:
            candidates |= self.postings.get(gram, set())
        if not candidates:
            return []

        scored = []
        for doc_id in candidates:
            score = cosine(counts, self.vectors[doc_id],
                           query_norm, self.norms[doc_id])
            if score > min_score:
                scored.append((doc_id, score))

        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [VectorHit(doc_id=doc_id, score=score,
                          url=self.docs[doc_id]["url"],
                          title=self.docs[doc_id]["title"])
                for doc_id, score in scored[:limit]]

    def stats(self) -> dict:
        return {"documents": len(self.vectors), "grams": len(self.postings),
                "postings": sum(len(d) for d in self.postings.values())}
