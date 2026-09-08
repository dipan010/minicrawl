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

POSITIONS (stage 18)

A posting stores WHERE each term occurs, not just how often. That is the only
way to answer a phrase query: "web crawler" must mean the two words adjacent
and in that order, not a document that mentions the web in paragraph one and a
crawler in paragraph nine. Term frequency is then just `len(positions)`, so
nothing is duplicated.

It costs, and the cost is worth measuring rather than repeating. The received
wisdom is that positions roughly triple an index; on this project's benchmark
corpus it is 1.6x, because most terms occur once in a document there and a
one-element list is barely larger than a count. Real prose repeats its words
more within a document, so the true figure sits between the two. The benchmark
prints what it actually measured, and says which corpus it measured it on.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tokenize import tokens

# The defaults every implementation ships with, and they are not arbitrary:
# k1=1.2 makes term frequency saturate quickly, b=0.75 applies most but not all
# of the length correction. Tuning them needs judged relevance data, which this
# project does not have — so they are left alone and said to be left alone.
K1 = 1.2
B = 0.75

# Positional distance inserted between fields. Any value larger than the
# longest phrase anyone will search for works; 1000 is far beyond that.
FIELD_GAP = 1000


@dataclass(slots=True)
class Query:
    terms: list[str] = field(default_factory=list)
    phrases: list[list[str]] = field(default_factory=list)


_QUOTED = re.compile(r'"([^"]*)"')


def parse_query(query: str) -> Query:
    """Split a query into loose terms and quoted phrases.

    An unterminated quote is treated as loose text rather than an error: a
    person who typed one wants results, not a syntax lecture.
    """
    phrases, rest = [], _QUOTED.sub(" ", query)
    for quoted in _QUOTED.findall(query):
        group = tokens(quoted)
        if len(group) == 1:
            # A "quoted" single word is just that word. Treating it as a
            # one-term phrase would work but adds a filter pass for nothing.
            rest += " " + quoted
        elif group:
            phrases.append(group)
    return Query(terms=tokens(rest), phrases=phrases)


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
        # term -> {doc_id: [positions]}. Term frequency is len(positions),
        # so the count is never stored twice and cannot disagree with itself.
        self.postings: dict[str, dict[int, list[int]]] = {}
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
        title_terms = tokens(title)
        body_terms = tokens(text)

        # A GAP between the fields, so no phrase can straddle a boundary. A
        # title ending "...Crawler" followed by a body starting "Politeness..."
        # would otherwise match the phrase "crawler politeness", which appears
        # nowhere in the document.
        #
        # The two title copies are separate fields for the same reason. Laid
        # end to end, a title of "Web Crawler" becomes web crawler web crawler
        # and invents the phrase "crawler web". Weighting a field must not
        # change which phrases the document contains.
        positions: dict[str, list[int]] = {}
        at = 0
        for group in (title_terms, title_terms, body_terms):
            for term in group:
                positions.setdefault(term, []).append(at)
                at += 1
            at += FIELD_GAP

        for term, where in positions.items():
            self.postings.setdefault(term, {})[doc_id] = where

        # Length is a term count, not the padded position span: the gap is a
        # positional device and must not make documents look longer than they
        # are, which would skew every BM25 score.
        self.docs.append({"url": url, "title": title, **meta})
        self.lengths.append(2 * len(title_terms) + len(body_terms))
        self.total_length += self.lengths[-1]
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

    def doc_frequency(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def term_frequency(self, term: str, doc_id: int) -> int:
        """How often a term occurs in a document — derived, never stored."""
        return len(self.postings.get(term, {}).get(doc_id, ()))

    def idf(self, term: str) -> float:
        """Inverse document frequency, the BM25+ variant.

        The classic Robertson/Sparck-Jones formula goes NEGATIVE for a term in
        more than half the documents, which lets a common word subtract from a
        score — a document can rank lower for containing a word you searched
        for. The +1 inside the log removes that without changing the ordering
        of anything else, and every serious implementation applies it.
        """
        n = self.doc_frequency(term)
        if n == 0:
            return 0.0
        return math.log(1 + (self.n_docs - n + 0.5) / (n + 0.5))

    # -- phrases -----------------------------------------------------------
    def phrase_docs(self, phrase_terms: list[str]) -> set[int]:
        """Documents containing these terms adjacent and in order.

        The classic positional intersection: start with the documents holding
        the first term, then for each subsequent term keep only positions that
        continue the run. A document survives only if some starting position
        reaches the end of the phrase.
        """
        if not phrase_terms:
            return set()
        first = self.postings.get(phrase_terms[0])
        if not first:
            return set()
        if len(phrase_terms) == 1:
            return set(first)

        matched = set()
        for doc_id, starts in first.items():
            running = set(starts)
            for offset, term in enumerate(phrase_terms[1:], start=1):
                where = self.postings.get(term, {}).get(doc_id)
                if not where:
                    running = set()
                    break
                ahead = set(where)
                # Keep only the starts whose (start + offset) is present.
                running = {s for s in running if s + offset in ahead}
                if not running:
                    break
            if running:
                matched.add(doc_id)
        return matched

    # -- querying ----------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[Hit]:
        """Rank documents for a query. This is the whole online system."""
        parsed = parse_query(query)
        terms = [t for group in parsed.phrases for t in group] + parsed.terms
        if not terms or not self.n_docs:
            return []

        # A quoted phrase is a FILTER, which is what quotes mean to a person:
        # show me documents containing exactly this. Its words still score
        # normally, so a document matching the phrase twice outranks one
        # matching it once.
        allowed: set[int] | None = None
        for group in parsed.phrases:
            found = self.phrase_docs(group)
            allowed = found if allowed is None else (allowed & found)
            if not allowed:
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
            for doc_id, where in postings.items():
                if allowed is not None and doc_id not in allowed:
                    continue
                frequency = len(where)
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
            "version": 2, "k1": self.k1, "b": self.b,
            "docs": self.docs, "lengths": self.lengths,
            "postings": {term: list(postings.items())
                         for term, postings in self.postings.items()},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Index":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        version = payload.get("version", 1)
        if version != 2:
            # A version-1 index stored counts, not positions. Loading it would
            # produce an index that answers term queries correctly and every
            # phrase query wrongly — the worst kind of compatibility, so it is
            # refused instead.
            raise ValueError(
                f"index format v{version} has no positions and cannot answer "
                "phrase queries; rebuild it with --build")
        index = cls(k1=payload["k1"], b=payload["b"])
        index.docs = payload["docs"]
        index.lengths = payload["lengths"]
        index.total_length = sum(index.lengths)
        index.postings = {term: {int(d): list(where) for d, where in postings}
                          for term, postings in payload["postings"].items()}
        return index

    def stats(self) -> dict:
        return {"documents": self.n_docs, "terms": len(self.postings),
                "postings": sum(len(p) for p in self.postings.values()),
                "positions": sum(len(where) for postings in self.postings.values()
                                 for where in postings.values()),
                "avg_length": round(self.avg_length, 1)}
