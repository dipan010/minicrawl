# Stage 17 — the inverted index, and why search is fast

This is the answer to the question the whole project has been circling.

**A search engine is not fast because its crawler is fast.** It is fast
because the crawling already happened, and what remains at query time is a
lookup in a structure built offline. Two systems with almost nothing in common:

| | | |
|---|---|---|
| **Offline** | crawl → extract → tokenise → index | slow, continuous, huge — stages 1–16 |
| **Online** | query → postings → score → rank | microseconds — this stage |

## The structure

An inverted index maps a term to the documents containing it. *Inverted*
because the obvious map goes the other way, document → its terms — and
searching that means reading every document.

```
"crawler" -> [(doc 3, tf 5), (doc 17, tf 2), ...]
```

Searching an inverted index means reading the postings for the query's terms
and nothing else. Documents containing none of them are never looked at. That
is the entire trick, and it means query cost tracks **how rare the words are**,
not how large the corpus is.

## The scoring

BM25, written out rather than imported. It is three ideas:

1. **Term frequency saturates.** A word appearing 50 times does not make a
   document 50× more relevant; `k1` controls how fast the tenth mention stops
   mattering. Without this you have not built ranking, you have built counting.
2. **Rare terms weigh more.** IDF. This is *why there is no stopword list*: a
   term in every document has an IDF near zero and contributes nothing on its
   own. Deleting stopwords is an optimisation from an era of expensive disks,
   and it breaks phrase queries — "to be or not to be" is entirely stopwords.
3. **Long documents are discounted.** They contain more of everything, so
   matching in one is less impressive; `b` controls how much of that
   correction applies.

One deviation from the textbook, and it matters. The classic
Robertson/Sparck-Jones IDF goes **negative** for a term in more than half the
corpus — so a document can rank *lower* for containing a word you searched
for. The `+1` inside the logarithm removes that without reordering anything
else. Every serious implementation applies it; the formula in most textbooks
does not.

## The measurement

`scripts/search_bench.py` runs the same BM25 twice — once over postings, once
over every document — at three corpus sizes:

| documents | build | terms | index p50 | scan p50 | speedup |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 0.03 s | 15,780 | **15 µs** | 216 µs | 14× |
| 10,000 | 0.36 s | 30,128 | **92 µs** | 3,011 µs | 33× |
| 50,000 | 2.00 s | 70,130 | **509 µs** | 31,240 µs | 61× |

Both structures are asserted to return identical rankings, because a faster
structure that gives different answers is not faster, it is wrong.

### The caveat is worth more than the headline

Over 50,000 documents, by how selective the query is:

| query | latency | postings touched |
|---|---:|---:|
| a term in 36 documents | **13 µs** | 36 |
| `duplicate detection simhash` | 503 µs | 2,289 |
| a term in 49,990 of 50,000 documents | **24,591 µs** | 49,990 |

The last row is the honest one. A term present in almost every document costs
almost exactly what a full scan costs, because its postings list *is* the
corpus. An inverted index is fast on **selective** queries, and `the` is not
one. Quoting only the 61× would be quoting the best case as if it were the
behaviour.

## The corpus nearly made the whole measurement meaningless

The first version of this benchmark built documents by resampling sixteen
sentences. Every term then appeared in roughly a third of all documents — the
worst case above, applied to *every* query. It measured a **2× speedup**, and I
had already written a summary line claiming cost tracks term rarity, which that
same table disproved.

Real text is Zipfian: a few words appear everywhere and most appear almost
nowhere, and that distribution is the entire reason an inverted index works. A
corpus without it does not test the structure at all.

This is stage 6 again, exactly. There, degenerate filler text made simhash
produce nonsense and the fix was to fix the corpus. The rule has now earned
itself a fourth outing: **if the corpus tolerates a wrong conclusion, the
corpus is wrong.** The benchmark now samples a 20,000-word vocabulary by rank,
and says so in its own docstring, because a synthetic corpus can be built to
prove anything.

## The tokeniser is where findability is decided

A term the tokeniser throws away is a term no query can ever match, which makes
`tokenize.py` quietly the most consequential file in the search path.

- **Case-folded**, so `Crawler` and `crawler` are one term.
- **Unicode-aware splitting**, so `naïve` and `日本語` survive. Splitting on
  ASCII word characters would silently drop most of the web.
- **NFKC-normalised**, so the ligature `ﬁle` matches the `file` a person types.
- **Digits kept** — version numbers, years and error codes are exactly what
  people search for.
- **Apostrophes folded in**, so `don't` is one term rather than `don` and `t`.
- **Absurdly long tokens dropped**: a 500-character "word" is a hash or
  minified markup, and indexing it costs a postings entry no query will match.

**No stemming.** `crawling` and `crawler` stay different terms, so searching
one will not find the other. That is a real loss of recall, and it is named as
a gap rather than smuggled in: a stemmer is a large table of rules whose
effects are invisible in the index, and BM25's behaviour is easier to see
without one.

## Where this meets stage 15

`Index.add_jsonl` reads the `--export` file directly. That was the point of
building export first — the JSONL is the seam, and the two stages meet at it
with no glue.

It skips records with no text. A 304 and a PDF are real crawled pages with
nothing to index, and admitting them as empty documents would change the
average document length, which changes **every** score in the index. A test
pins that.

## What was deliberately not built

- **A seekable on-disk index.** `save`/`load` write a JSON snapshot and read
  the whole thing back. A real engine stores delta-encoded compressed postings
  blocks with a term dictionary you binary-search — the shape `cdx.py` already
  demonstrates — so a query reads a few kilobytes of a file that never fits in
  memory. Honest at this size, a lie at any other, and the docstring says so.
- **Phrase and proximity queries.** Positions are not stored, only counts, so
  `"web crawler"` cannot be distinguished from a document containing both
  words far apart. Storing positions is the change; it roughly triples the
  index.
- **An HTTP search endpoint.** The CLI queries the index today. Serving it
  belongs with the retrieval work, where there is something worth serving.
- **Tuned `k1` and `b`.** Tuning them requires judged relevance data, which
  this project does not have. They are left at 1.2 and 0.75 and *said* to be
  left there, rather than presented as a choice.
