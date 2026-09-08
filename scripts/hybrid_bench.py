"""What hybrid retrieval buys, and what it costs.

Two retrievers that fail in opposite directions. The useful question is not
"is hybrid better" — it is: which queries does each one MISS, does fusing them
rescue those, and does it damage the ones a single retriever already answered?

All three are reported, including the last, because a fusion that improves
recall by wrecking precision is not an improvement.

    uv run python scripts/hybrid_bench.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicrawl.crawler import CrawlConfig, crawl                  # noqa: E402
from minicrawl.dedup import DuplicateIndex                        # noqa: E402
from minicrawl.export import JsonlExporter                        # noqa: E402
from minicrawl.hybrid import HybridSearcher                       # noqa: E402
from testsite import server as testsite_server                    # noqa: E402
from testsite import spec                                         # noqa: E402

# Words that appear in the corpus, paired with a form nobody indexed. BM25
# cannot match the right-hand column by construction; that is the point.
MORPHOLOGY = [
    ("redirects", "redirect"),
    ("duplicate", "duplicates"),
    ("normalisation", "normalise"),
    ("politeness", "polite"),
    ("compressed", "compression"),
    ("generated", "generator"),
]

# Queries where n-grams are expected to be WRONG: letters in common, nothing
# else. Reported so the cost is visible next to the benefit.
SPURIOUS = ["spider", "crawlspace", "rediscover"]


async def build(tmp: Path) -> HybridSearcher:
    exporter = JsonlExporter(tmp / "pages.jsonl")
    await crawl(CrawlConfig(seeds=[f"http://{spec.host(spec.PRIMARY)}/"],
                            max_pages=60, max_depth=4,
                            dedup=DuplicateIndex(), exporter=exporter,
                            on_page=None))
    return HybridSearcher.build(tmp / "pages.jsonl")


def timed(fn, repeats: int = 20) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1e6)
    return statistics.median(samples)


def main() -> int:
    servers = testsite_server.serve(skip_busy=True)
    try:
        tmp = Path(tempfile.mkdtemp())
        searcher = asyncio.run(build(tmp))
        n = searcher.index.n_docs

        print(f"\n  {n} documents · {len(searcher.index.postings)} terms · "
              f"{len(searcher.ngrams.postings)} n-grams\n")

        print("  morphology — the same word, a form nobody indexed\n")
        print(f"  {'indexed form':<16} {'query':<16} {'bm25':>6} {'ngram':>7} "
              f"{'fused':>7}   rescued")
        print("  " + "-" * 64)
        rescued, eligible = 0, 0
        for indexed, query in MORPHOLOGY:
            base = len(searcher.index.search(indexed, limit=10))
            bm25 = len(searcher.index.search(query, limit=10))
            ngram = len(searcher.ngrams.search(query, limit=10))
            fused = len(searcher.search(query, limit=10))

            # A rescue only counts when the INDEXED form is really in the
            # corpus. If BM25 finds nothing for either spelling, the word is
            # simply absent, and n-grams returning something is noise being
            # counted as recall — the easiest way to make a fusion look good.
            if base == 0:
                note = "not in corpus"
            else:
                eligible += 1
                saved = bm25 == 0 and fused > 0
                rescued += saved
                note = "yes" if saved else "—"
            print(f"  {indexed:<16} {query:<16} {bm25:>6} {ngram:>7} {fused:>7}"
                  f"   {note:<14} (exact form: {base})")
        print(f"\n  {rescued} of {eligible} queries where the word IS indexed "
              f"but BM25\n  could not match the spelling were rescued by "
              f"fusion.\n")

        print("  where n-grams are simply wrong\n")
        for query in SPURIOUS:
            hits = searcher.ngrams.search(query, limit=3)
            top = ", ".join(f"{h.title or h.url.rsplit('/', 1)[-1]}"
                            f" {h.score:.2f}" for h in hits) or "nothing"
            print(f"  {query:<14} {len(hits)} hits   {top}")
        print("\n  Letters in common are not meaning in common. Fusion keeps")
        print("  these low because BM25 ranks them nowhere, which is exactly")
        print("  the disagreement RRF is for.\n")

        print("  does fusion damage what BM25 already answered?\n")
        kept = 0
        checked = [q for q, _ in MORPHOLOGY] + ["café", "robots", "trap"]
        for query in checked:
            bm25 = [h.doc_id for h in searcher.index.search(query, limit=5)]
            if not bm25:
                continue
            fused = [h.doc_id for h in searcher.search(query, limit=5)]
            if bm25[0] in fused:
                kept += 1
            marker = "kept" if bm25[0] in fused else "LOST"
            print(f"  {query:<16} bm25 top-1 {marker} in the fused top 5")
        print(f"\n  {kept} preserved.\n")

        print("  cost\n")
        query = "duplicate detection"
        print(f"  bm25 alone     {timed(lambda: searcher.index.search(query)):>8.0f}µs")
        print(f"  n-grams alone  {timed(lambda: searcher.ngrams.search(query)):>8.0f}µs")
        print(f"  fused          {timed(lambda: searcher.search(query)):>8.0f}µs")
        print("\n  Hybrid costs both retrievers plus the fusion, which is the")
        print("  honest price: it is two searches, not a cleverer one.\n")
    finally:
        for httpd in servers:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
