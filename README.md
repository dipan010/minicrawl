# minicrawl

A web crawler built from scratch in Python, one concept at a time, against a
synthetic corpus engineered to punish every shortcut.

The point is not to produce another crawler — Scrapy exists. The point is that
by stage 10 you can read Scrapy's source and recognise every piece of it,
because you wrote a worse version of each one first.

## The ladder

Each stage is a git tag and a note in `docs/`. Nothing is checked in until it
is verified against the ground-truth manifest.

| Stage | Adds | Concept it proves |
|------:|------|-------------------|
| 1 ✅ | `fetch.py`, `extract.py` | HTTP semantics, body caps, `<base href>`, URL resolution + validation |
| 2 ✅ | `crawler.py`, `frontier/memory.py` | The crawl loop, BFS, cycle avoidance, scope |
| 3 ✅ | `robots.py`, `politeness.py` | robots.txt (hand-rolled), crawl-delay, per-host rate limits |
| 4 ✅ | `frontier/hosted.py`, worker pool | Concurrency that does not become a DoS |
| 5 | `normalize.py`, `frontier/sqlite.py` | Canonicalisation, dedup, resumable crawls, trap escape |
| 6 | `extract.py` main-text, `dedup.py` | Boilerplate removal, exact + near-dup (simhash) |
| 7 | `render.py` | Escalating to Playwright *only* for pages that need it |
| 8 | `freshness.py`, sitemaps | Conditional GET, recrawl scheduling, priority frontier |
| 9 | `frontier/redis.py` | Distributed coordination, host-sharded workers |
| 10 | `scrapy_port/` | What the framework actually buys you |

## Run it

```bash
uv sync --extra dev

# terminal 1 — the corpus, on four origins
uv run python -m testsite.server

# terminal 2 — crawl it, and diff against ground truth
uv run minicrawl http://127.0.0.1:8081/ --max-pages 40 --verify

# concurrency across hosts, politeness within each one
uv run minicrawl http://127.0.0.1:808{1,2,4}/ --workers 8 --max-pages 60

uv run pytest -q
```

## How correctness is decided

`testsite/spec.py` declares the corpus: the page graph, the robots rules, which
URLs are spellings of the same resource, which pages are duplicates, which are
JS-only. `testsite/manifest.py` walks that declaration — no HTTP, no HTML
parsing — and writes `testsite/manifest.json`, including `expected_pages`: the
**19 pages** a polite same-host crawl from `/` must find, exactly.

The crawler has to reach the same answer the hard way. That gap is the test.

At stage 2 the diff reads:

```
expected 19 pages, crawled 32 on 127.0.0.1:8081
0 missing, 13 extra     # /private/secret + 12 pages of generator trap
```

Zero missing means link extraction is correct. The extras are the honest
scoreboard of what stages 3 and 5 still have to fix.

## Known gaps, on purpose

`tests/test_stage02_bfs.py` ends with `test_characterises_*` tests that assert
the crawler's *current* failures — no URL normalisation, walks into the
generator trap. Each one is designed to break when the stage that fixes it
lands. Flipping a characterisation test is the definition of done for a stage.

Stage 3 flipped the first one: `test_characterises_no_robots_support_yet`
asserted that `/private/secret` got fetched, and is now
`test_disallowed_pages_are_never_fetched` asserting the opposite.
