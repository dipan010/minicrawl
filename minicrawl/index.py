"""Stage 17 — the inverted index, and why search is fast.

This is the answer to the question the whole project has been circling. A
search engine is not fast because its crawler is fast. It is fast because
**the crawling already happened**, and what remains at query time is a lookup
in a structure built offline.

Two systems, and they have almost nothing in common:

    OFFLINE   crawl -> extract -> tokenise -> index.  Slow, continuous, huge.
              Stages 1 through 16 built this.
    ONLINE    query -> postings -> score -> rank.  Microseconds. This file.

THE STRUCTURE

An inverted index is a map from term to the documents containing it — inverted
because the obvious map goes the other way. Searching a forward index means
reading every document; searching an inverted one means reading the postings
for the query's terms and nothing else. That is the entire trick, and it is why
a query costs time proportional to how RARE the words are rather than how big
the corpus is.

    "crawler" -> [(doc 3, tf 5), (doc 17, tf 2), ...]

THE SCORING

BM25 is what everybody actually uses, and it is three ideas:

  1. A term appearing 50 times does not make a document 50x more relevant.
     Term frequency SATURATES, controlled by k1 — the tenth mention adds
     almost nothing over the ninth.

  2. A term in every document tells you nothing. IDF weights rare terms up and
     common ones down, which is why no stopword list is needed here: "the" has
     an IDF near zero and contributes nothing on its own.

  3. A long document contains more of everything, so matching in it is less
     impressive. Length normalisation, controlled by b, discounts it.

Written out rather than imported. It is fifteen lines, and the point of this
project is to know why the tenth mention does not count.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .tokenize import counted, tokens

# The defaults every implementation ships with, and they are not arbitrary:
# k1=1.2 makes term frequency saturate quickly, b=0.75 applies most but not all
# of the length correction. Tuning them needs judged relevance data, which this
# project does not have — so they are left alone and said to be left alone.
K1 = 1.2
B = 0.75


@dataclass(slots=True)
class Hit:
    doc_id: int
    score: float
    url: str
    title: str
    matched: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        parts = " + ".join(f"{term} {score:.3f}"
                           for term, score in sorted(self.matched.items(),
                                                     key=lambda kv: -kv[1]))
        return f"{self.score:.3f} = {parts}"


class Index:
    """An in-memory inverted index with BM25 scoring.

    In memory because that is what makes the lesson visible: the cost of a
    query is postings traversal, and nothing else is in the way. `save`/`load`
    persist it, and the docstring on `save` is honest about what that format
    is not.
    """

    def __init__(self, k1: float = K1, b: float = B):
        self.k1 = k1
        self.b = b
        # term -> {doc_id: term frequency}
        self.postings: dict[str, dict[int, int]] = {}
        self.docs: list[dict] = []            # metadata, indexed by doc_id
        self.lengths: list[int] = []          # in terms, indexed by doc_id
        self.total_length = 0

    # -- building ----------------------------------------------------------
    def add(self, text: str, *, url: str = "", title: str = "",
            **meta) -> int:
        """Index one document. Returns its id."""
        doc_id = len(self.docs)
        # The title is worth indexing, and worth indexing TWICE: a page whose
        # title is "Web crawler" is more about crawlers than one that mentions
        # the phrase once in its footer. This is the poor relation of proper
        # field weighting (BM25F), and calling it that is more honest than
        # pretending a single flat field is a considered choice.
        frequencies = counted(f"{title} {title} {text}" if title else text)
        length = sum(frequencies.values())

        for term, count in frequencies.items():
            self.postings.setdefault(term, {})[doc_id] = count

        self.docs.append({"url": url, "title": title, **meta})
        self.lengths.append(length)
        self.total_length += length
        return doc_id

    def add_jsonl(self, path: str | Path, *, text_field: str = "text") -> int:
        """Index a stage-15 export. This is the seam the two stages meet at."""
        added = 0
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                body = record.get(text_field)
                if not body:
                    continue          # a 304 or a PDF: no text to index
                self.add(body, url=record.get("final_url") or record.get("url", ""),
                         title=record.get("title", ""))
                added += 1
        return added

    # -- the numbers -------------------------------------------------------
    @property
    def n_docs(self) -> int:
        return len(self.docs)

    @property
    def avg_length(self) -> float:
        return self.total_length / self.n_docs if self.n_docs else 0.0

    def idf(self, term: str) -> float:
        """Inverse document frequency, the BM25+ variant.

        The classic Robertson/Sparck-Jones formula goes NEGATIVE for a term in
        more than half the documents, which lets a common word subtract from a
        score — a document can rank lower for containing a word you searched
        for. The +1 inside the log removes that without changing the ordering
        of anything else, and every serious implementation applies it.
        """
        n = len(self.postings.get(term, ()))
        if n == 0:
            return 0.0
        return math.log(1 + (self.n_docs - n + 0.5) / (n + 0.5))

    # -- querying ----------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[Hit]:
        """Rank documents for a query. This is the whole online system."""
        terms = tokens(query)
        if not terms or not self.n_docs:
            return []

        avg = self.avg_length or 1.0
        scores: dict[int, float] = {}
        detail: dict[int, dict[str, float]] = {}

        # Only the postings for the query's terms are ever touched. Documents
        # containing none of them are never looked at — which is why cost
        # tracks how RARE the words are, not how large the corpus is.
        for term in set(terms):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for doc_id, frequency in postings.items():
                length_ratio = self.lengths[doc_id] / avg
                # Saturation and length normalisation, in one line each.
                denominator = frequency + self.k1 * (1 - self.b + self.b * length_ratio)
                contribution = idf * (frequency * (self.k1 + 1)) / denominator
                scores[doc_id] = scores.get(doc_id, 0.0) + contribution
                detail.setdefault(doc_id, {})[term] = contribution

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [Hit(doc_id=doc_id, score=score,
                    url=self.docs[doc_id].get("url", ""),
                    title=self.docs[doc_id].get("title", ""),
                    matched=detail[doc_id])
                for doc_id, score in ranked]

    # -- persistence -------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        """Write the index to one file.

        This is a snapshot, NOT the seekable on-disk format a real engine uses.
        A production index stores postings as delta-encoded, compressed blocks
        with a term dictionary you binary-search — the shape `cdx.py` already
        demonstrates — so a query reads a few kilobytes of a file that never
        fits in memory. Here the whole thing is loaded, which is honest at this
        size and would be a lie at any other.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1, "k1": self.k1, "b": self.b,
            "docs": self.docs, "lengths": self.lengths,
            "postings": {term: list(postings.items())
                         for term, postings in self.postings.items()},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Index":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        index = cls(k1=payload["k1"], b=payload["b"])
        index.docs = payload["docs"]
        index.lengths = payload["lengths"]
        index.total_length = sum(index.lengths)
        index.postings = {term: {int(d): int(f) for d, f in postings}
                          for term, postings in payload["postings"].items()}
        return index

    def stats(self) -> dict:
        return {"documents": self.n_docs, "terms": len(self.postings),
                "postings": sum(len(p) for p in self.postings.values()),
                "avg_length": round(self.avg_length, 1)}
