# Stage 19 — hybrid retrieval

Stage 17 named a failure and left it there: BM25 matches **terms**, so a query
for `redirect` cannot find a document that says `Redirects`. Those are two
different strings, and the document is invisible.

This stage fixes it — and the first thing to say is what the fix is *not*.

## What this is not

The obvious answer is "add embeddings". A dense vector from a trained model
would know that `crawler` and `spider` mean the same thing, and that is a
genuinely different capability.

It also costs a model. `torch` alone is larger than every dependency this
project has combined, against a core of two, and a stage that installs two
gigabytes to demonstrate an idea has stopped demonstrating it.

More to the point: **the failure I promised to fix is morphological, not
semantic.** `redirect` and `Redirects` are the same word. Reaching for
embeddings to solve that would be solving a different problem than the one the
corpus actually has, and hoping nobody checked.

So this stage adds character n-grams — which fix morphology exactly, cost
nothing, and are honestly bad at meaning. `docs/stage-19.md` says so and a test
asserts it, because "hybrid semantic search" is a phrase that sells better than
it describes.

## Character n-grams

A word becomes the little sequences of letters it contains:

```
crawler  ->  ^cr  cra  raw  awl  wle  ler  er$
```

`crawlers` shares six of those seven. `redirect` and `redirects` score **0.83**
against each other; `redirect` and `crawler` score **0.0**.

The `^` and `$` markers matter more than they look. Without them, `ler` in
`crawler` and `ler` in `lerner` are indistinguishable, and prefixes stop
counting for anything — `redirect` would match `predirect` as readily as
`redirects`.

Similarity is **cosine**, because two documents about the same thing at
different lengths have proportional n-gram counts rather than equal ones.
Cosine compares direction and ignores magnitude: the same problem BM25 solves
with length normalisation, solved differently.

And n-grams get their own postings, gram → documents, so only documents sharing
at least one gram are ever scored. Comparing the query against every document
would be the forward scan that stage 17 spent an entire stage avoiding.

## Fusing two retrievers that disagree

The obvious combination is `a * bm25 + b * cosine`. It does not work, and the
reason is worth understanding: **the two numbers are not comparable.** A BM25
score is unbounded, corpus-dependent and grows with query length. A cosine is
in `[0, 1]` by construction. Adding them means choosing weights that are really
a guess about scale, and the guess must be retuned whenever the corpus changes.

**Reciprocal Rank Fusion** discards the scores and keeps only the order:

```
score(d) = Σ over retrievers  1 / (k + rank(d))
```

Rank is comparable across anything. A document ranked first contributes the
same amount whether its retriever reported `14.2` or `0.83`. RRF needs no
tuning, no normalisation and no knowledge of either scoring function — and it
beats most carefully weighted combinations in the published comparisons, which
is either humbling or liberating.

`k = 60` (Cormack et al., 2009) is deliberately large: the gap between rank 1
and rank 2 is small, so a retriever must be confident across *several*
positions to dominate. A small `k` lets one retriever's top hit win every
time, which is single retrieval wearing a hat. A test pins that relationship.

## What it buys, measured

`scripts/hybrid_bench.py`, over the crawled corpus:

| indexed form | query | bm25 | n-gram | fused | rescued |
|---|---|---:|---:|---:|---|
| redirects | `redirect` | 0 | 10 | 10 | yes |
| duplicate | `duplicates` | 0 | 10 | 10 | yes |
| normalisation | `normalise` | 0 | 10 | 10 | *not in corpus* |
| politeness | `polite` | 2 | 9 | 9 | — |
| compressed | `compression` | 0 | 10 | 10 | yes |
| generated | `generator` | 0 | 10 | 10 | yes |

**4 of 5.** And the excluded row is the interesting one.

`normalisation` returns nothing for *either* spelling, because the word is not
in the corpus at all. Counting that as a rescue would have made the headline
5 of 6 — n-grams returning noise, scored as recall. The benchmark now checks
that the indexed form really is present before a query is eligible, which is
the easiest possible way to make a fusion look better than it is, and I had
written it that way first.

## What it costs, also measured

| | |
|---|---:|
| bm25 alone | 6 µs |
| n-grams alone | 27 µs |
| fused | 42 µs |

Hybrid is two searches plus a fusion. It is not a cleverer search; it is more
work, and the price belongs next to the benefit.

The other cost is precision, and it is visible:

```
spider       ->  Page B 0.23, Mislabelled café 0.14, Client rendered 0.11
crawlspace   ->  Duplicate 0.18, Duplicate 0.18, Page B 0.18
rediscover   ->  Page A 0.17, Page A 0.17, Page A 0.17
```

Letters in common are not meaning in common. Fusion keeps these low precisely
*because* BM25 ranks them nowhere — the disagreement is the mechanism, not a
problem with it.

And the regression check that matters: for every query BM25 could already
answer, its top result is still in the fused top five. **7 of 7 preserved.** A
fusion that improves recall by wrecking precision is not an improvement.

## What was deliberately not built

- **Dense semantic embeddings.** The real thing, and the honest reason it is
  absent is dependency weight rather than difficulty. It would fix
  `crawler`/`spider`, which nothing here can. If it were added, RRF would take
  it as a third ranking with no other change — which is the nicest property
  fusion has.
- **A reranker.** Cross-encoding the top 50 is what production systems do after
  fusion, and it needs a model for the same reason.
- **Learned weights.** Tuning fusion needs judged relevance data. This project
  has ground truth about *crawling* and none about relevance, and inventing
  judgements to tune against would be marking my own homework.
- **Stemming.** Still absent, and now less necessary. A stemmer is a table of
  rules per language; n-grams need none and work on the whole corpus.
