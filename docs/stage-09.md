# Stage 9 — one crawl, many processes

**Added:** `minicrawl/frontier/redis.py` (`RedisFrontier`, `RedisPoliteness`), `scripts/distributed_demo.py`
**Changed:** `Politeness` now answers in durations; `SchedulingFrontier` counts work globally
**Verify:** `uv run pytest tests/test_stage09_distributed.py -q` (12 tests, 2 need Redis)
**Run:** `docker run -d --rm -p 6379:6379 redis:7-alpine && uv run python scripts/distributed_demo.py 3`

```
3 processes, 6.6s

  process 0:  9 pages fetched, 0 errors, 0 reclaimed
  process 1:  7 pages fetched, 0 errors, 0 reclaimed
  process 2: 10 pages fetched, 1 errors, 0 reclaimed

  fetched      26 requests, 26 distinct URLs
  duplicates   0  <-- the dedup guarantee held
  coverage     complete
  politeness   26 requests to :8081 in 6.6s; Crawl-delay floor is 5.0s -> held
```

Three OS processes, no shared memory, coordinating only through Redis.

## What was actually free before

Every earlier stage assumed one process owns the frontier. That assumption is
invisible until you want a second one, and then it is everywhere. Three
guarantees that cost nothing in one process have to be bought in many:

**Dedup.** The seen-set was a Python `set`. Now it is a Redis `SET`, and the
important part is that `SADD`'s return value *is* the decision — atomically.
Checking membership and then adding is two round trips with a gap in the
middle, and in that gap two workers both conclude they were first.

**One request per host.** The in-flight guard was a Python `set`, which only
this process can see. Now it is a **lease**: `SET key NX PX`, atomic by
construction, held by a named worker, with a TTL.

**Politeness.** The per-host clock was a dict of monotonic timestamps. Three
processes each obeying `Crawl-delay: 0.2` locally hit the origin three times
faster than it asked for — each of them politely. The limit belongs to the host
being crawled, so the clock has to live where every process can see it.

That last one forced a small refactor. Monotonic clocks have no shared epoch,
so they are meaningless across processes, and a shared clock must use the wall
clock. Rather than special-case it, `Politeness` now answers
`seconds_until_ready(host)` — a **duration**, not an absolute time — and each
implementation keeps its clock wherever it likes. The scheduler stopped caring.

The honest cost: wall clocks drift. NTP skew between machines makes the crawler
slightly ruder or slightly slower. A monotonic clock would make it simply
wrong.

## And one guarantee that has to be engineered

**Recovery.** A process that dies while holding a request must not take that
request with it.

The lease has a TTL; the request it covers does not. So a `proc:<host>` entry
with no matching `lease:<host>` is the only surviving evidence that work was
handed out to somebody who is now gone. `reclaim()` sweeps for exactly that and
requeues it, and every crawl runs the sweep at startup.

Without it a crashed worker silently drops its page and the crawl reports
success. That is the same class of bug as stage 6's `finally` block recording
work that never happened — and it is the class this project keeps producing,
because a missing page looks like nothing at all.

## Termination gets harder

"The queue is empty" was never "the crawl is done" — a worker still holding a
request may be about to enqueue more. Across processes, that worker may not be
in this process at all, so `pending` and `active` both became Redis counters.
`SchedulingFrontier` asks `_pending_total()` / `_active_total()`, and the local
frontiers answer from their own integers exactly as before.

## Polling, reluctantly

A local frontier wakes its workers with a condition variable. No such thing
crosses a process boundary, so a shared frontier has to look again on its own.
`poll_interval` exists solely for that, is set only by `RedisFrontier`, and is
`None` everywhere else — which keeps the local frontiers exactly as event-driven
as they were.

## Ordering, and a detail that bites

The shared queue is a ZSET scored by priority. A ZSET orders equal scores
**lexicographically**, not by insertion — so equal priorities would come back in
alphabetical order, silently replacing FIFO with something arbitrary. The member
carries a zero-padded arrival sequence as a prefix, which restores FIFO within a
priority band.

## The interface, three implementations later

`RedisFrontier` implements the same four storage hooks as the deque and the
SQLite table, plus `_pending_total` / `_active_total` and a `poll_interval`.
`SchedulingFrontier` — the readiness clock, the one-per-host guard, the worker
waking, the termination condition — is untouched.

Stage 2 declared the frontier as an interface when there was exactly one
implementation and no argument for the abstraction. This is the third backend,
and the crawl loop still does not know which one it has.
