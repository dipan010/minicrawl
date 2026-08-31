# Stage 5 — one resource, one URL; one crawl, many processes

**Added:** `normalize.py`, `traps.py`, `frontier/scheduling.py`, `frontier/sqlite.py`
**Changed:** `HostedFrontier` reduced to storage; scheduling moved to a shared base
**Verify:** `uv run pytest tests/test_stage05_normalize_persist.py -q` (27 tests)
**Flipped:** both remaining characterisation tests

```
expected 19 pages, crawled 24 on 127.0.0.1:8081
bounded     5 trap pages under /gen/ — capped, not excluded
refused     4 shape_budget
exact match against the manifest
                          ... stopped: frontier drained
```

`frontier drained` is the sentence this whole stage exists to produce. Stages
2–4 only ever stopped because `max_pages` ran out.

## The rule I had to take back out

The first version of `normalize_path` stripped trailing slashes, on the
reasonable-sounding grounds that `/a` and `/a/` are usually the same page. It
broke the corpus immediately: `/docs/` became `/docs`, which **404s**, because
a directory-style route is a real thing on real servers. A normaliser that
strips the slash does not deduplicate URLs, it invents 404s.

The correct mechanism belongs to the server, not the crawler: it answers `/a/`
with a 301 to `/a`, and the crawler learns the two are one by following the
redirect and recording where it landed. One request buys an authoritative
answer instead of a guess. The corpus was changed to 301 rather than silently
serve both spellings, because the previous behaviour was letting a wrong
normaliser look right.

So `/a`'s fourteen spellings collapse to **three** fetches, not two:

| | |
|---|---|
| 8 bare spellings | → `/a` by normalisation, free |
| 5 queried spellings | → `/a?a=1&b=2` by normalisation, free |
| `/a/` | → `/a` by 301, costs one request |

Two distinct pages come back from the three. `mark_seen()` records the landing
URL so *later* discoveries are free — a URL already queued when the redirect
resolves cannot be un-queued, which is why the number is three and not two.

## What normalisation is allowed to do

Only transformations RFC 3986 says cannot change which resource is addressed:
lowercase scheme and host, drop the default port, resolve dot segments, drop
the fragment, normalise percent-encoding of unreserved characters, sort query
parameters, and drop parameters that identify the *visitor* rather than the
resource (`utm_*`, `sid`, `fbclid`, …).

Not allowed, and deliberately absent: dropping `index.html`, lowercasing the
path, stripping the trailing slash. Each is a guess about server behaviour
dressed up as a canonical form.

And the query is normalised, not discarded. `/a?a=1&b=2` is not `/a`. A
normaliser that drops every query string looks impressively aggressive on a
test corpus and destroys most of a real site.

## Traps: counting shapes, not URLs

Normalisation cannot touch `/gen/1`, `/gen/2`, `/gen/3`… Every one of those
URLs is genuinely distinct and addresses a genuinely different page. The crawl
is not confused, it is being *fed*.

What catches it is counting **shapes**: replace whole numeric path segments
with a placeholder, and `/gen/1` and `/gen/9999` become one `/gen/<num>`. A
budget per shape caps any single generated family without a rule that names
`/gen`, and without capping the crawl as a whole. Only whole-numeric segments
collapse, so `/dup/exact-1` and `/dup/exact-2` stay distinct.

The budget is spent only on URLs the frontier does not already know — otherwise
a popular page linked from everywhere burns the budget a genuinely new URL
needs.

Three cheaper guards catch the classic variants: `max_path_depth` for calendars
that link to next month forever, `max_repeated_segment` for symlink loops,
`max_query_params` for faceted search.

This bounds the trap at 5 pages; it does not eliminate it, and `--verify`
reports those 5 as **bounded** rather than unexpected. A general defence can
cap a generator, it cannot know to exclude one. Stage 6 removes them properly,
on content — every `/gen/*` page is a near-duplicate of every other.

## Persistence, and the statement that matters

The table *is* the seen-set: `url` is the primary key, so `INSERT OR IGNORE`
does deduplication and durability in one statement.

```
state = queued | in_flight | done
```

Recovery is one statement at open:

```sql
UPDATE frontier SET state='queued' WHERE state='in_flight'
```

Any row still `in_flight` belongs to a worker that no longer exists. Without
this, every crash silently drops exactly the requests that were in progress —
which are, by selection, the slow and difficult ones. The test forces the case
by marking rows `in_flight` behind the crawler's back and reopening.

```
run 1 (cap 8)          fetched= 8  already_done= 0  stopped: max_pages (8) reached
run 2 (resume)         fetched=19  already_done= 9  stopped: frontier drained
run 3 (nothing left)   fetched= 0  already_done=31  stopped: frontier drained
```

`sqlite3` is synchronous and these calls block the event loop. At this scale
each is microseconds against a local file — cheaper than the thread hop that
avoiding it would cost. Stage 9 moves the frontier out of the process entirely
and the question stops being ours.

## The refactor that paid off the stage-2 decision

Adding a second frontier required no new scheduling code. The readiness clock,
one-in-flight-per-host, and worker waking moved to `SchedulingFrontier`, and
storage became four hooks — `_add`, `_take`, `_hosts_with_work`, `_complete`.
`HostedFrontier` and `SqliteFrontier` implement those and nothing else, and
`test_both_frontiers_produce_the_same_crawl` asserts they are interchangeable.

Declaring the frontier as an interface at stage 2, before there was any second
implementation to justify it, is what made this a refactor instead of a rewrite.
