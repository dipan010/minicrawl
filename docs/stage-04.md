# Stage 4 — the worker pool

**Added:** `minicrawl/frontier/hosted.py`, worker pool in `crawler.py`, `--workers`
**Changed:** `Politeness.wait()` split into `ready_at()` + `mark_used()`
**Verify:** `uv run pytest tests/test_stage04_concurrency.py -q` (14 tests)

## The mistake this stage exists to avoid

Put eight workers on one shared FIFO and watch what happens. They all draw
URLs from whichever host is at the head of the queue, all block on that host's
`Crawl-delay`, and every other host sits idle. The crawl ends up both slower
*and* ruder than the sequential version. Concurrency and politeness are
constraints on different axes, and one queue cannot express both.

So the queue has to know about hosts:

- one FIFO per host, all sharing **one** seen-set, so dedup stays global even
  though ordering does not
- at most one in-flight request per host, ever
- a worker is handed a request only from a host whose delay has already elapsed
- when no host is ready, workers sleep until the **earliest** one is, not a
  fixed poll interval

`acquire()` / `release()` bracket a request. `push()` is async because adding
work has to be able to wake a sleeping worker — and `notify_all()` requires the
condition lock, which is why seeding uses a separate `push_nowait()`. Calling
the async path before any worker exists raises `RuntimeError`, which is how
that split was discovered.

## Politeness had to be taken apart

Stage 3's `wait()` did two things at once: sleep until the host is ready, then
claim it. That is exactly wrong under concurrency — a worker that sleeps has
given up its chance to go serve a *different* host. Splitting it into
`ready_at()` (a question) and `mark_used()` (a claim) lets the frontier make
that decision instead of the worker.

The clock still starts when a request **begins**, not when it ends, so a slow
response does not earn the crawler extra waiting on top of it. Same semantics
as stage 3; the test that pins it is unchanged.

## Results

**One host — concurrency must buy nothing:**

```
workers= 1   4.89s  pages=19  peak_in_flight=1
workers= 8   4.86s  pages=19  peak_in_flight=1
```

This is the stage passing, not failing. `Crawl-delay: 0.2` is the binding
constraint and no number of workers may get around it. A crawler that got
faster here would be broken.

**Three hosts — now it buys exactly the host count:**

```
workers= 1   6.29s  pages=57  peak_in_flight=1
workers= 8   4.85s  pages=57  peak_in_flight=3
```

The most interesting number is the 6.29s. A single worker over three hosts is
*already* far faster than 57 × 0.2s = 11.4s, because the host-partitioned
frontier lets one worker switch hosts rather than sleep. Most of the win here
comes from the queue structure, not from the parallelism — the workers only
add what the network I/O was costing.

Sixteen workers is identical to eight. You cannot exceed one request per host,
so the host count is the real ceiling and everything above it is idle workers.

## The metric that was lying

The first version reported `slept_for_politeness: 62.96s` on a 4.86s crawl.
It was summing idle time across all workers — true, but useless, and framed as
if the crawl had spent a minute being polite. Renamed to
`worker_seconds_waiting`, documented as an aggregate. Eight workers and three
hosts means five workers are always waiting; that is the arithmetic working,
not a problem. A metric that reads as nonsense will be believed by whoever
reads it next, so it has to be named for what it measures.

## Budgets under concurrency

`max_pages` is reserved **before** the fetch, not after. Check it afterwards
and eight workers all pass the check on the last slot, overshooting by eight
pages. asyncio is cooperative — nothing preempts between `await`s — so a plain
`int` is a sound budget here without a lock, and `test_max_pages_is_not_overshot_by_the_worker_count`
pins that at 1, 4 and 16 workers.

## What the tests actually assert

Not "it got faster" — speed is a property of the corpus and the machine. The
tests assert structure:

- never more than one in-flight request per host, at 16 workers
- peak concurrency ≤ host count
- a single host does not speed up, no matter the worker count
- **a hanging host does not stall the crawl** — seeded with `/hang` (30s) and a
  second origin, the second origin is fully crawled and the run finishes in
  under 5s. This is the strongest evidence the workers are genuinely independent.
- the *set* of pages found is identical at 1 and 8 workers, even though the
  order is not
