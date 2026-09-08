# Stage 18 — positions, and the phrase queries they buy

Stage 17 stored how *often* a term occurred. This stores **where**, which is
the only way to answer a phrase query: `"web crawler"` must mean those two
words adjacent and in that order, not a document mentioning the web in
paragraph one and a crawler in paragraph nine.

Term frequency becomes `len(positions)` — derived, never stored twice, and so
it cannot disagree with itself.

## The positional intersection

Start with the documents holding the first term. For each subsequent term,
keep only the starting positions whose `start + offset` also appears. A
document survives if any start reaches the end of the phrase.

```
"quick brown"
  quick -> doc 3 at [4, 19]
  brown -> doc 3 at [5, 40]
  4+1 = 5 is present  ->  doc 3 matches
```

A quoted phrase is a **filter**, which is what quotes mean to a person: show me
documents containing exactly this. Its words still score normally afterwards,
so a document containing the phrase twice outranks one containing it once —
and a loose term alongside a phrase changes the *order*, never the membership.
Quoting one part of a query must not silently drop results matching the rest.

## Field boundaries, and a phrase the index invented

Positions need gaps between fields, or phrases straddle them. A title ending
`…Crawler` followed by a body starting `Politeness…` would match
`"crawler politeness"` — a phrase appearing nowhere in the document.

The subtler case was of my own making. Stage 17 weighted titles by indexing
them twice, which was fine for counting. With positions, laying the two copies
end to end turns a title of `Web Crawler` into

```
web crawler web crawler
```

and manufactures the phrase `"crawler web"`. The document does not contain it;
the weighting scheme invented it.

So the two title copies are separate fields, each followed by a `FIELD_GAP`.
**Weighting a field must not change which phrases a document contains** — and
it is worth noticing that this bug did not exist until positions did. A design
that is harmless under one representation can be wrong under the next.

Document length stays a term count and does not include the gaps. If padding
leaked into length, every document would look longer than it is and *every*
BM25 score would shift.

## What positions cost, measured rather than repeated

The received wisdom is that positions roughly triple an index. On this
project's benchmark corpus:

| | |
|---|---|
| 11,934,520 bytes | counts only (stage 17) |
| 19,173,825 bytes | with positions (stage 18) — **1.6×** |
| 1.38 | positions per posting |

Not 3×, and the reason matters: this corpus samples a vocabulary by rank, so
most terms occur **once** in a document, and a one-element list is barely
larger than a count. Real prose repeats its words more within a document, so
the true figure sits between 1.6 and 3. The benchmark prints what it measured
and names the corpus it measured on, rather than quoting the folklore I had
already written into the docstring.

## What the filter buys

Three queries over the same words, 10,000 documents:

| query | latency | documents |
|---|---:|---:|
| `crawler simhash` | 167 µs | 474 — either word, anywhere |
| `"duplicate detection"` | 140 µs | 153 — adjacent, in order |
| `"detection duplicate"` | 47 µs | 0 — the same words, reversed |

The reversed phrase is the useful row: same terms, same postings, no results,
and *faster*, because the intersection collapses early.

## Refusing an old index instead of mis-answering it

A stage-17 index stored counts. Loading one now would produce something that
answers term queries correctly and every phrase query wrongly — the worst kind
of compatibility, because nothing looks broken.

`Index.load` therefore checks the version and refuses, naming the fix:

```
index format v1 has no positions and cannot answer phrase queries;
rebuild it with --build
```

Silently degrading would have been friendlier and worse.

## Two of my own tests were wrong

Both asserted rankings I had guessed rather than derived.

`crawler robots` over `"crawler robots"` (2 terms) and
`"crawler crawler robots url"` (4 terms): I expected the second to win, having
twice the occurrences of `crawler`. It does score higher **on that term** —
and loses, because saturation caps what the second mention adds while length
normalisation keeps discounting the longer document.

That is BM25 behaving exactly as designed, and I had written the test as
though it were a counter. Both tests were rewritten to assert properties
rather than orderings, and the behaviour I got wrong is now a test of its own:
`test_a_shorter_document_can_beat_more_occurrences`.

## What was deliberately not built

- **Proximity search** (`NEAR/5`). The machinery is here — the intersection
  already compares offsets — and it is a genuinely useful operator. It is one
  parameter away and belongs with a real query language rather than bolted
  onto quotes.
- **Slop.** `"web crawler"~2` allowing two words between. Same argument.
- **Skip pointers.** The intersection walks whole postings lists. A real
  engine stores skip lists so a rare term can leap through a common one's
  postings — worthwhile at a corpus size this project does not have.
- **Positions as deltas.** Storing `[4, 19, 40]` as `[4, 15, 21]` compresses
  well, and combined with variable-byte encoding is where most of the 1.6×
  would go back. That is index compression, a stage in its own right.
