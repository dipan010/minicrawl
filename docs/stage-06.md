# Stage 6 — main text, and when two pages are the same page

**Added:** `minicrawl/dedup.py`, `main_text()` in `extract.py`, dedup in the crawl loop
**Verify:** `uv run pytest tests/test_stage06_content_dedup.py -q` (20 tests)
**Fixed:** a silent data-loss bug in the stage-5 frontier, found by this stage

> **Corpus size at this tag:** ground truth was 19 pages here. `/volatile`
> arrived at stage 8 and took it to 20, so the figures below reproduce at
> this stage's tag, not at `HEAD`.

```
expected 19 pages, crawled 22 on 127.0.0.1:8081
bounded     3 trap pages under /gen/ — capped, not excluded
documents   17 unique, 2 exact dup, 3 near dup, 1 canonical alias, 2 refetched
exact match against the manifest
```

Stage 5 bounded the generator at the shape budget of 5. Stage 6 stops it at 3,
on content — and **the URL guard never fires at all**.

## Three questions, three tools

| Question | Tool |
|---|---|
| byte-for-byte identical? | SHA-256 of the normalised text |
| nearly the same? | simhash + Hamming distance |
| declared the same? | `rel=canonical` |

The middle one is the only hard one, because equality-based structures cannot
answer it. A hash table finds exact matches; "differs by six words out of four
hundred" is not a match.

**Simhash is a locality-sensitive hash** — similar inputs get similar hashes,
the exact opposite of what a cryptographic hash promises. Shingle the text into
overlapping 4-word windows (so word order survives; a bag of words calls "dog
bites man" and "man bites dog" identical), hash each shingle, let every bit
column vote ±1 per shingle, keep the sign. Change a few shingles and a few
votes move, flipping at most a few bits. Similarity becomes a number.

**Banding** makes lookup cheap. Comparing against every stored fingerprint is
O(n) per page, which is the cost simhash exists to avoid. Split the 64 bits
into B bands and index each separately: two fingerprints differing in fewer
than B bits must, by the pigeonhole principle, agree exactly on some band.

That principle is a hard constraint, not a tuning knob. **Widening
`max_distance` past `BANDS` does not make the index slower — it makes it
wrong**, silently failing to find the pairs it exists to find. The threshold
had to move from 3 to 6 for this corpus, so the bands moved from 4×16 to 8×8,
and a test asserts `max_distance < BANDS` so the coupling cannot be broken by
someone tuning one number.

## Boilerplate removal is not tidiness

Navigation, headers and footers are identical on every page of a site. Left in,
they make every page look similar to every other — precisely the signal
duplicate detection is trying to read. `main_text()` strips them.

It mutates the parse tree, so it must run *after* link extraction: the links
live in the `<nav>` it deletes. A test pins that ordering, because reversing it
would silently return zero links.

## The threshold is measured, not looked up

The folklore constant for 64-bit simhash is 3. It is calibrated for documents
with thousands of shingles, and it does not transfer. Measured on this corpus:

```
declared duplicate pairs
   /dup/exact-1   vs /dup/exact-2   distance= 0   (37 words)
   /dup/near-1    vs /dup/near-2    distance= 4   (450 words, 2 words edited)
   /gen/1         vs /gen/2         distance= 0
closest non-pair
   /variants      vs /gen/1         distance=24
```

Threshold 6 sits in a margin between 4 and 24. An earlier version of the near
pair — six words edited out of 188 — measured **11 bits apart**, above any
threshold that would still exclude unrelated pages. The relationship is
proportional: the same edit is a large fraction of a short document and a
negligible one of a long document. A test asserts the margin, so the corpus
cannot drift into being unable to test what it claims to test.

## Three corpus flaws this stage exposed

Every one of them let a wrong implementation look right.

**1. The generator filler was degenerate, and it broke simhash outright.**
The filler was one phrase repeated, giving a 65 KB page with **four distinct
shingles**, the top two occurring 3,839 and 3,838 times. Near-equal weights
cancel in every bit column, so the ±1 contributions of four differing shingles
decided the sign — two pages differing by one word in 7,679 came out **14 bits
apart**. Fixed by making the filler varied prose, which is what real generated
pages carry. The finding became a guard: `MIN_DISTINCT_SHINGLES`. A document
can be enormous and still be unfit for a fingerprint, so length alone is not a
sufficient test.

**2. `/a` shared its paragraph with the exact-duplicate pair**, differing only
in the heading — measured distance 4. It *was* a near-duplicate, and no
threshold should be asked to pretend otherwise. `/a` got its own text.

**3. `/dup/exact-2` was reachable only through `/dup/near-1`.** Since this
stage suppresses link-following on duplicates, one ordering would have made
`exact-2` permanently unreachable. Linked from the index as well, so the
coverage risk is visible rather than accidental.

## The risk this stage takes on

Not following a duplicate's links is what actually kills a generated family:
the chain dies at the first repeat instead of at a budget. But **a page
reachable only through a duplicate becomes unreachable**. That is a real
coverage cost, accepted deliberately, and `follow_duplicate_links=True` exists
so the control case can be run — with suppression off, the chain runs to 5
again and the stage-5 URL guard is what stops it.

## The bug this stage found in stage 5

`follow_duplicate_links` was declared in the config and never read — the flag
did nothing, which a control test caught immediately.

The serious one was underneath it. When a worker hits `max_pages` it stops
without processing the request it is holding. The `finally` block released that
request as **finished**, which in the SQLite frontier marked it `done` — so
every later resume skipped it. A page silently lost from every
capped-then-resumed crawl, and invisible unless you compare the union of two
runs against ground truth, which is exactly what the resume test does.

`release(url, done=False)` now puts the request back. The lesson is narrow and
worth keeping: **a `finally` block that records completion will happily record
work that never happened.**
