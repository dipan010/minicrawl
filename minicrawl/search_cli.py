"""`minicrawl-search` — build an index from a crawl, then query it.

    minicrawl-search --build pages.jsonl --index idx.json
    minicrawl-search --index idx.json "robots exclusion"
    minicrawl-search --index idx.json --explain "robots exclusion"
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .hybrid import HybridSearcher
from .index import Index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="minicrawl-search",
        description="Build a BM25 index from a crawl export, and search it.")
    ap.add_argument("query", nargs="*",
                    help='words to search for; "quote a phrase" to require '
                         'those words adjacent and in order')
    ap.add_argument("--build", metavar="JSONL",
                    help="build the index from a --export file")
    ap.add_argument("--index", metavar="PATH",
                    help="where the index lives (written by --build, else read)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--explain", action="store_true",
                    help="show each term's contribution to the score")
    ap.add_argument("--hybrid", metavar="JSONL",
                    help="also match on character n-grams, fusing the two "
                         "rankings; takes the export to build both from")
    args = ap.parse_args(argv)
    if not args.index and not args.hybrid:
        ap.error("--index is required (or use --hybrid with an export)")

    if args.hybrid:
        # Hybrid builds both retrievers from the export in one pass, so it
        # does not read --index at all.
        searcher = HybridSearcher.build(args.hybrid)
        if not args.query:
            ap.error("give something to search for")
        query = " ".join(args.query)

        started = time.perf_counter()
        hits = searcher.search(query, limit=args.limit)
        micros = (time.perf_counter() - started) * 1e6

        comparison = searcher.compare(query, limit=args.limit)
        print(f"\n{len(hits)} results for {query!r} in {micros:.0f}µs "
              f"over {searcher.index.n_docs:,} documents")
        print(f"  bm25 found {len(comparison['bm25'])}, "
              f"n-grams found {len(comparison['ngram'])}, "
              f"{len(comparison['only_ngram'])} only n-grams reached\n")
        for rank, hit in enumerate(hits, 1):
            print(f"{rank:>2}. {hit.title or '(no title)'}")
            print(f"      {hit.url}")
            if args.explain:
                print(f"      {hit.explain()}")
        return 0 if hits else 1

    if args.build:
        index = Index()
        started = time.perf_counter()
        added = index.add_jsonl(args.build)
        elapsed = time.perf_counter() - started
        index.save(args.index)
        stats = index.stats()
        print(f"indexed {added} documents in {elapsed:.2f}s — "
              f"{stats['terms']:,} terms, {stats['postings']:,} postings, "
              f"{stats['positions']:,} positions, "
              f"avg {stats['avg_length']} terms/doc")
        print(f"  {args.index} ({Path(args.index).stat().st_size:,} bytes)")
        if not args.query:
            return 0
    elif not Path(args.index).exists():
        ap.error(f"{args.index} does not exist — build it first with --build")

    if not args.query:
        ap.error("give something to search for, or use --build")

    index = Index.load(args.index) if not args.build else index
    query = " ".join(args.query)

    started = time.perf_counter()
    hits = index.search(query, limit=args.limit)
    micros = (time.perf_counter() - started) * 1e6

    print(f"\n{len(hits)} results for {query!r} "
          f"in {micros:.0f}µs over {index.n_docs:,} documents\n")
    for rank, hit in enumerate(hits, 1):
        print(f"{rank:>2}. {hit.score:6.3f}  {hit.title or '(no title)'}")
        print(f"      {hit.url}")
        if args.explain:
            print(f"      {hit.explain()}")
    if not hits:
        print("  nothing matched.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
