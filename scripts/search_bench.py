"""Why search is fast: an inverted index against a forward scan.

The claim is not that this implementation is quick. It is that the DATA
STRUCTURE is what makes query time small, and that the difference grows with
the corpus. So the same BM25 scoring runs twice — once over postings for the
query's terms, once over every document — and both are timed at several sizes.

THE CORPUS HAS TO BE REALISTIC OR THE MEASUREMENT IS MEANINGLESS

The first version of this benchmark built documents by resampling sixteen
sentences. Every term then appeared in roughly a third of all documents, which
is the WORST case for an inverted index: the postings lists are nearly as long
as the corpus, so traversing them costs about what scanning everything costs.
It measured a 2x speedup and I nearly published that, alongside a summary line
claiming cost tracks term rarity — which the same table disproved.

Real text is Zipfian: a few words appear everywhere, and most appear almost
nowhere. That distribution is the entire reason an inverted index works, so a
corpus without it does not test the structure at all. This is the same lesson
stage 6 learned when degenerate filler text broke simhash — if the corpus
tolerates a wrong conclusion, the corpus is wrong.

So: a 20,000-word vocabulary sampled by Zipf's law, plus themed sentences so
that meaningful queries match something. Stated here rather than buried,
because a synthetic corpus can be built to prove anything.

    uv run python scripts/search_bench.py
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicrawl.index import B, K1, Index                          # noqa: E402
from minicrawl.tokenize import counted, tokens                    # noqa: E402

QUERIES = ["w19999", "w9000 w15000", "duplicate detection simhash",
           "robots", "café", "w0", "the"]

SENTENCES = [
    "A crawler fetches a page and extracts the links it contains.",
    "The robots exclusion protocol tells a crawler what it may not fetch.",
    "Politeness means one request per host at a time, spaced by a delay.",
    "Duplicate detection uses simhash to find near duplicates cheaply.",
    "A redirect chain means the URL you asked for is not the one you got.",
    "Normalisation collapses many spellings of a URL into one canonical form.",
    "An inverted index maps a term to the documents that contain it.",
    "BM25 saturates term frequency and normalises for document length.",
    "The frontier decides which URL a worker fetches next.",
    "A WARC file stores the whole HTTP response, headers included.",
    "Content addressing names an object by the hash of its bytes.",
    "Conditional requests use an ETag so unchanged pages cost nothing.",
    "Character encoding must be decided, not assumed, or text becomes mojibake.",
    "A café menu in windows-1252 decodes wrongly if you assume UTF-8.",
    "Sitemaps are a second source of seeds, independent of following links.",
    "Trap defence counts URL shapes rather than URLs.",
]


class ForwardScan:
    """The obvious structure: every document's terms, scanned per query.

    This is what an index is an alternative TO. It gives identical results —
    the scoring is the same function — and it reads the whole corpus to
    produce them.
    """

    def __init__(self, k1: float = K1, b: float = B):
        self.k1, self.b = k1, b
        self.docs: list[dict[str, int]] = []
        self.lengths: list[int] = []

    def add(self, text: str) -> None:
        frequencies = counted(text)
        self.docs.append(frequencies)
        self.lengths.append(sum(frequencies.values()))

    def search(self, query: str, limit: int = 10):
        terms = set(tokens(query))
        n = len(self.docs)
        avg = (sum(self.lengths) / n) if n else 1.0
        # Document frequencies, needed for IDF, computed the only way a forward
        # index can: by looking at every document.
        df = {term: sum(1 for doc in self.docs if term in doc) for term in terms}
        scored = []
        for doc_id, doc in enumerate(self.docs):
            score = 0.0
            for term in terms:
                frequency = doc.get(term, 0)
                if not frequency:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                ratio = self.lengths[doc_id] / avg
                score += idf * (frequency * (self.k1 + 1)) / (
                    frequency + self.k1 * (1 - self.b + self.b * ratio))
            if score:
                scored.append((doc_id, score))
        return sorted(scored, key=lambda kv: -kv[1])[:limit]


VOCAB_SIZE = 20_000


def make_corpus(n: int, seed: int = 7) -> list[str]:
    """N documents whose term frequencies follow Zipf's law, as real text does.

    Each document is mostly vocabulary sampled by rank (weight 1/rank), with
    one themed sentence mixed in so the readable queries below match real
    documents rather than nothing.
    """
    rng = random.Random(seed)
    vocabulary = [f"w{i}" for i in range(VOCAB_SIZE)]
    weights = [1.0 / (i + 1) for i in range(VOCAB_SIZE)]
    corpus = []
    for i in range(n):
        words = rng.choices(vocabulary, weights=weights, k=rng.randint(60, 200))
        themed = rng.choice(SENTENCES) if rng.random() < 0.25 else ""
        corpus.append(f"Document {i}. {themed} " + " ".join(words))
    return corpus


def timed(fn, repeats: int = 30) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1e6)
    samples.sort()
    return {"p50": samples[len(samples) // 2],
            "p95": samples[min(len(samples) - 1, int(0.95 * len(samples)))],
            "mean": statistics.fmean(samples)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 50_000])
    args = ap.parse_args()

    print("\n  query latency, microseconds — same BM25, two structures\n")
    print(f"  {'documents':>10} {'build':>9} {'terms':>8} "
          f"{'index p50':>11} {'index p95':>11} {'scan p50':>11} {'speedup':>9}")
    print("  " + "-" * 76)

    for n in args.sizes:
        corpus = make_corpus(n)

        started = time.perf_counter()
        index = Index()
        for i, text in enumerate(corpus):
            index.add(text, url=f"http://example.test/{i}", title=f"Document {i}")
        build = time.perf_counter() - started

        scan = ForwardScan()
        for text in corpus:
            scan.add(text)

        # A selective query, which is what search is actually for. The
        # common-word case is reported separately below rather than averaged
        # in, because the two behave completely differently.
        query = "duplicate detection simhash"
        fast = timed(lambda: index.search(query), repeats=30)
        slow = timed(lambda: scan.search(query), repeats=3)

        # Same question, same answer — a faster structure that returns
        # something else is not faster, it is wrong.
        assert [h.doc_id for h in index.search(query)] == \
               [doc_id for doc_id, _ in scan.search(query)], "structures disagree"

        print(f"  {n:>10,} {build:>8.2f}s {len(index.postings):>8,} "
              f"{fast['p50']:>10.0f}µ {fast['p95']:>10.0f}µ "
              f"{slow['p50']:>10.0f}µ {slow['p50'] / fast['p50']:>8.0f}x")

    print("\n  by selectivity, over 50,000 documents\n")
    corpus = make_corpus(50_000)
    index = Index()
    for i, text in enumerate(corpus):
        index.add(text, url=f"http://example.test/{i}", title=f"Document {i}")
    for query in QUERIES:
        result = timed(lambda q=query: index.search(q), repeats=30)
        hits = index.search(query)
        matched = sum(len(index.postings.get(t, ())) for t in set(tokens(query)))
        print(f"  {query!r:<34} {result['p50']:>8.0f}µs   "
              f"{matched:>9,} postings touched   {len(hits)} shown")

    print("\n  Cost tracks the length of the postings lists touched — that is,")
    print("  how rare the query's words are. A term in almost every document")
    print("  costs almost what a scan costs, which is the honest caveat: an")
    print("  inverted index is fast on SELECTIVE queries, and 'the' is not one.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
