# minicrawl

A web crawler built from scratch in Python, one concept at a time, against a
synthetic corpus engineered to punish every shortcut.

The point is not to produce another crawler — Scrapy exists. The point is that
by stage 10 you can read Scrapy's source and recognise every piece of it,
because you wrote a worse version of each one first.

## Two kinds of notes

`docs/stage-NN.md` is **why** — narrative rationale, the judgement calls, the
rules that had to be taken back out. Read it to understand a decision.

`logs/stage-NN/notes.txt` is **what and how** — the steps in the order they
happened, the concepts stated independently of this codebase, and an explicit
concept → code map naming the file and function each idea is made of. Read it
to study the theory or to retrace the work. Every stage gets one, written as
the stage lands.

## The ladder — complete

Ten stages, ten tags, 151 tests. Each stage is a git tag, a rationale note in
`docs/`, and a working log in `logs/`. Nothing was checked in until it verified
against the ground-truth manifest.

| Stage | Adds | Concept it proves |
|------:|------|-------------------|
| 1 ✅ | `fetch.py`, `extract.py` | HTTP semantics, body caps, `<base href>`, URL resolution + validation |
| 2 ✅ | `crawler.py`, `frontier/memory.py` | The crawl loop, BFS, cycle avoidance, scope |
| 3 ✅ | `robots.py`, `politeness.py` | robots.txt (hand-rolled), crawl-delay, per-host rate limits |
| 4 ✅ | `frontier/hosted.py`, worker pool | Concurrency that does not become a DoS |
| 5 ✅ | `normalize.py`, `traps.py`, `frontier/sqlite.py` | Canonicalisation, dedup, resumable crawls, trap defence |
| 6 ✅ | `dedup.py`, `main_text()` | Boilerplate removal, exact + near-dup (simhash), canonical |
| 7 ✅ | `render.py` | Escalating to Playwright *only* for pages that need it |
| 8 ✅ | `freshness.py`, `sitemap.py`, `PriorityQueue` | Conditional GET, recrawl scheduling, priority frontier |
| 9 ✅ | `frontier/redis.py` | Distributed coordination, leases, shared politeness |
| 10 ✅ | `scrapy_port/`, `scripts/compare_frameworks.py` | What the framework actually buys you |

## Run it

```bash
uv sync --extra dev

# optional, for stage 7 only (~150MB)
uv sync --extra render && uv run playwright install chromium

# terminal 1 — the corpus, on four origins
uv run python -m testsite.server

# terminal 2 — crawl it, and diff against ground truth
uv run minicrawl http://127.0.0.1:8081/ --max-pages 40 --verify

# concurrency across hosts, politeness within each one
uv run minicrawl http://127.0.0.1:808{1,2,4}/ --workers 8 --max-pages 60

# a resumable crawl: interrupt it, run it again, it picks up where it stopped
uv run minicrawl http://127.0.0.1:8081/ --frontier crawl.sqlite3 --verify

# escalate to a browser only where triage says it would help (1 page in 26)
uv run minicrawl http://127.0.0.1:8081/ --render

# sitemaps + conditional GET: run it three times and watch the bytes vanish
uv run minicrawl http://127.0.0.1:8081/ --sitemaps --freshness fresh.sqlite3

# one crawl split across three processes, coordinating only through Redis
docker run -d --rm --name minicrawl-redis -p 6379:6379 redis:7-alpine
uv sync --extra distributed
uv run python scripts/distributed_demo.py 3

# the punchline: this crawler vs Scrapy, same corpus, every number measured
uv sync --extra scrapy
uv run python scripts/compare_frameworks.py

uv run pytest -q
```

## How correctness is decided

`testsite/spec.py` declares the corpus: the page graph, the robots rules, which
URLs are spellings of the same resource, which pages are duplicates, which are
JS-only. `testsite/manifest.py` walks that declaration — no HTTP, no HTML
parsing — and writes `testsite/manifest.json`, including `expected_pages`: the
**19 pages** a polite same-host crawl from `/` must find, exactly.

The crawler has to reach the same answer the hard way. That gap is the test.

At stage 2 the diff read:

```
expected 19 pages, crawled 32 on 127.0.0.1:8081
0 missing, 13 extra     # /private/secret + 12 pages of generator trap
```

At stage 5 it reads:

```
expected 19 pages, crawled 24 on 127.0.0.1:8081
bounded     5 trap pages under /gen/ — capped, not excluded
exact match against the manifest
                          ... stopped: frontier drained
```

`frontier drained` is the point. Stages 2–4 only ever stopped because
`max_pages` ran out. The 5 remaining trap pages are bounded rather than absent:
a general defence can cap a generator, it cannot know to exclude one. Stage 6
removes them on content.

## The punchline

`scripts/compare_frameworks.py` runs this crawler and a Scrapy port of it
against the same corpus:

```
                            reqs  pages  /gen  /a spellings   secs
minicrawl (stage 5)           29     29     5             2    6.5
scrapy (defaults)            247    246   223             4    1.8
scrapy + minicrawl.traps      25     24     3             2    1.1
```

Both find every expected page. Scrapy is faster because minicrawl is slower on
purpose — it honours the `Crawl-delay: 0.2` the site states, which Scrapy never
reads. Against a site asking for 49 seconds of spacing, the default spider took
1.8. And where `robots.txt` answers HTTP 500, RFC 9309 says assume a complete
disallow: minicrawl fetches 0 pages, Scrapy fetches 251.

None of that is a bug in Scrapy. Each is a default doing what it says, and
noticing them is the whole point of having built the thing once. See
`docs/stage-10.md`.

## Known gaps, on purpose

`tests/test_stage02_bfs.py` ends with `test_characterises_*` tests that assert
the crawler's *current* failures — no URL normalisation, walks into the
generator trap. Each one is designed to break when the stage that fixes it
lands. Flipping a characterisation test is the definition of done for a stage.

All three have now been flipped:

| Asserted at stage 2 | Flipped by | Now asserts |
|---|---|---|
| `no_robots_support_yet` | stage 3 | `test_disallowed_pages_are_never_fetched` |
| `no_url_normalisation_yet` | stage 5 | `test_fourteen_spellings_of_a_become_three_fetches` |
| `generator_trap_is_entered` | stage 5 | `test_generator_trap_is_bounded` |
