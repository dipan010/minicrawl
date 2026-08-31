# Stage 8 — freshness: what to fetch again, and how to fetch it cheaply

**Added:** `minicrawl/sitemap.py`, `minicrawl/freshness.py`, `PriorityQueue`, conditional GET
**Verify:** `uv run pytest tests/test_stage08_freshness.py -q` (17 tests)
**Corpus:** `/orphan` (linked from nowhere, listed in a sitemap), `/volatile` (changes every request), ETags on every page

```
run 1: 27 pages   0 × 304   210,556 bytes downloaded        coverage complete
run 2: 29 pages  26 × 304   131,813 bytes downloaded        coverage complete
run 3: 29 pages  28 × 304         186 bytes downloaded      coverage complete
```

186 bytes on the third pass, and that is `/volatile` — the one page that
genuinely changed.

## Two problems that get conflated

**How to recheck** is conditional GET: send back the validator the server gave
you and let it answer `304` with no body. A round trip instead of a page.

**When to recheck** is scheduling. A homepage changes hourly; an archived
article never changes again. One fixed interval wastes requests on one and
misses updates on the other.

The scheduling rule is the classic adaptive one — **unchanged doubles the
interval, changed halves it**, clamped at both ends. It converges on each
page's rhythm without storing history, and its failure modes are stated rather
than hidden: it reacts slowly to a page that suddenly starts changing, and it
never lets a stable page go unchecked beyond `max_interval` no matter what.

First sight is deliberately not a change. There is nothing to compare against,
and counting it would halve the interval of every page the crawler has never
seen.

## The finding that reshaped the stage

The first working version crawled **27 pages on run 1 and 10 on run 2**.

A 304 carries no body. No body means no links. A crawler that discovers only by
parsing responses **goes blind the moment its cache starts working** — it 304s
the homepage, never sees the homepage's links, and never enqueues anything
behind it. Conditional GET and link discovery are in direct tension.

The resolution is that the freshness store keeps each page's **outlinks**, and
a 304 re-pushes them. That is not an optimisation; without it, caching and
crawling cannot both be on. It is also why real crawlers keep a link graph
rather than only a document store.

## Sitemaps are a second, independent seed source

`/orphan` is listed in a sitemap and linked from nowhere. No depth limit, no
politeness setting and no parsing cleverness reaches it — the control test
asserts that a link-following crawl cannot, which is what makes the sitemap
crawl finding it mean anything.

A `<sitemapindex>` is a list of other sitemaps, so following one is a small
crawl of its own, with the same trap potential as any other. The recursion is
bounded. A malformed sitemap yields nothing rather than raising: it is a hint,
not a contract, and plenty of real ones are truncated or are an HTML error page
served with an XML content type.

## The heap, and an honest result

Stage 2 said a heap instead of a deque turns breadth-first crawling into
priority crawling and that nothing else has to change. `PriorityQueue` is that
claim cashed: identical interface, six lines of difference, swapped by a
constructor argument.

**On this corpus it changes nothing, and that is the correct outcome.** With
`priority == depth` on a single-seed crawl, priority order *is* arrival order,
so a heap and a FIFO agree by construction. I tried to build a crawl-level
demonstration where they diverge and could not do it honestly — every source of
work here is known before the workers start.

The tests therefore show the divergence where it is real: push work whose
priority does not match its arrival order, and the two orderings differ. That
is the condition — something important discovered *after* less important work
is already queued — and it is what a heap is for.

## The bug that chase exposed

Recrawl seeds were being given `record.next_due`, a raw Unix timestamp around
1.7 × 10⁹. Discovered links were being given `depth`, a small integer. Both
went into the same heap.

Every newly discovered link therefore outranked every overdue page by a factor
of a billion, and a recrawl would abandon the work it was asked to do the
moment it found a link. Priority is now documented in one unit — **seconds from
now at which this should ideally be fetched**, negative meaning overdue — and a
regression test asserts that every due page is fetched before any link
discovered from one.

A priority queue is only as coherent as the scale its producers agree on, and
nothing in the type system was going to catch two producers disagreeing.

## A measurement error worth recording

Comparing FIFO against the heap on recrawl order, the two gave different
answers and the heap looked wrong. It was not: the FIFO run had **re-recorded**
the pages it fetched, pushing their `next_due` later, so the second run started
from a different schedule. The comparison mutated the thing being compared.
Fixed by running each ordering against its own copy of the store.
