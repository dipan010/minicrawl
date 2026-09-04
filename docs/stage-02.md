# Stage 2 — the crawl loop

**Added:** `minicrawl/crawler.py`, `minicrawl/frontier/{base,memory}.py`, `minicrawl/cli.py`
**Verify:** `uv run minicrawl http://127.0.0.1:8081/ --max-pages 40 --verify`

```
seed → frontier → fetch → extract → push links → repeat
```

Everything in stages 3–9 is a refinement of those five boxes.

## The frontier is an interface from day one

`frontier/base.py` declares `push` / `pop` / `seen_count` and nothing else.
Stage 2 backs it with a `deque`, stage 5 with SQLite, stage 9 with Redis, and
`crawler.py` never learns which. Declaring this at stage 2 rather than
discovering it at stage 9 is the single highest-leverage decision in the repo.

FIFO makes the crawl breadth-first — shallow and broad rather than tunnelling
down one path. A heap instead of a deque turns it into priority crawling
(stage 8).

> **Corpus size at this tag:** ground truth was 19 pages here. `/volatile`
> arrived at stage 8 and took it to 20, so the figures below reproduce at
> this stage's tag, not at `HEAD`.

## The result

```
expected 19 pages, crawled 32 on 127.0.0.1:8081
0 missing, 13 extra
```

**0 missing** — link extraction, redirect following and `<base href>`
resolution are all correct. This is the part that is finished.

**13 extra** — `/private/secret` (robots.txt is not read yet) and twelve pages
of `/gen/*` (nothing recognises a generator trap). Both are stated shortcomings,
not surprises.

Also visible in the log: `/a` is fetched **ten times**, once per spelling,
because the seen-set holds raw URL strings. Worse, the unnormalised `/a/`
spelling makes the relative link `c` resolve to `/a/c`, a URL that does not
exist — a phantom 404 invented by the crawler itself. That is stage 5's
headline, and it is why normalisation is a correctness feature and not an
optimisation.

## The band-aids, named as such

`max_pages` and `max_depth` are not features. They exist because this stage has
no way to survive `/gen/*`, and they are what stops the crawl instead of the
frontier draining naturally. Stage 5 removes the need for them.

## Characterisation tests

Three tests in `tests/test_stage02_bfs.py` assert the crawler's current
failures — it fetches robots-disallowed pages, it refetches `/a`, it enters the
trap. Each is written to **fail** when the stage that fixes it lands. Flipping
one is how a stage is declared done.
