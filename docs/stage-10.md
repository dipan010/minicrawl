# Stage 10 — what the framework actually buys you

**Added:** `scrapy_port/`, `scripts/compare_frameworks.py`
**Verify:** `uv sync --extra scrapy && uv run python scripts/compare_frameworks.py`
**Tests:** `uv run pytest tests/test_stage10_scrapy_port.py -q` (6 tests)

Everything below is measured against the same corpus, not recalled.

```
                            reqs  pages  /gen  /a spellings   secs
  ----------------------------------------------------------------
minicrawl (stage 5)           29     29     5             2    6.5
scrapy (defaults)            247    246   223             4    1.8
scrapy + minicrawl.traps      25     24     3             2    1.1

  coverage against the manifest (20 pages expected)
    all three                  complete

  Crawl-delay: :8081 asks for 0.2s between requests
    minicrawl                  29 reqs in 6.5s (floor 5.6s)  -> honoured
    scrapy (defaults)         247 reqs in 1.8s (floor 49.2s) -> NOT honoured

  robots.txt returning HTTP 500 (:8083)
    minicrawl                    0 pages fetched
    scrapy (defaults)          251 pages fetched
```

## Start with what Scrapy simply gives you

Written honestly, because the list is long and the point of the exercise is not
to win.

| Setting | Replaces |
|---|---|
| `ROBOTSTXT_OBEY` | `robots.py` + `RobotsCache` — and Protego agrees with my parser on every rule in the corpus |
| `CONCURRENT_REQUESTS_PER_DOMAIN` | the one-in-flight-per-host guard in `frontier/scheduling.py` |
| `DEPTH_LIMIT`, `CLOSESPIDER_PAGECOUNT` | `max_depth`, `max_pages` |
| the scheduler + dupefilter | `frontier/` — three modules |
| `HTTPCACHE_*` | most of `freshness.py` |
| `SitemapSpider` | `sitemap.py` |
| feed exports, retries, autothrottle, telnet console, stats | nothing I wrote at all |

Plus everything not on the list: a plugin architecture, contracts, `scrapy
shell`, deployment, and fifteen years of other people's edge cases.

**Scrapy found every page.** So did minicrawl. On the thing a crawler is for,
the framework wins on effort by a wide margin.

## Where the defaults diverge, and by how much

**The generated family: 223 pages against 5.** Scrapy ships no trap defence of
any kind, and `DEPTH_LIMIT` does not substitute for one — it bounds a crawl, it
does not recognise that a site is manufacturing pages. Importing `minicrawl.traps`
into the spider is a one-line change and takes it to 3, which is the useful half
of the finding: the gap is real, and closing it is cheap *once you know what to
write*. That is what nine stages bought.

**URL canonicalisation: 4 spellings against 2.** `w3lib.canonicalize_url` sorts
query parameters and drops the fragment, but keeps `utm_source` and `sid`, so
the corpus's five queried spellings of `/a` stay three URLs instead of becoming
one. Correct-but-conservative — dropping parameters can change meaning, and a
general-purpose library is right to be careful. Knowing *which* parameters are
safe to drop is site knowledge, and it is the crawler author's job.

**Crawl-delay: not honoured.** This is the serious one. `DOWNLOAD_DELAY` is a
constant *you* pick; Scrapy never reads the delay the site states. AutoThrottle
adapts to latency, which protects throughput rather than the origin's stated
wishes. Against a site asking for 0.2s between requests, the default spider
issued 247 requests in 1.8 seconds where the site asked for 49.

minicrawl is **slower** here — 6.5s against 1.8s — and it is slower precisely
because it is polite. That is the honest way round.

**robots.txt that fails: 251 pages against 0.** RFC 9309 §2.3.1.4 says a 5xx or
unreachable robots.txt means assume a complete disallow. Scrapy's middleware
allows the crawl when it cannot fetch the file. The parsers agree perfectly on
the rules themselves — a test pins that — so the divergence is entirely in what
each does when the rules cannot be read.

## The bug that cost the most time

Two of them, both the same shape, and both arguments for the same thing.

**`start_requests()` is dead code in Scrapy 2.13+.** It was replaced by an async
`start()`. An override of the old name is never called: no error, no warning, no
deprecation notice. The spider ran, reported success, and crawled zero pages.

**`allowed_domains` cannot scope to an origin.** `get_host_regex()` builds a
pattern *including* the port; `should_follow()` matches it against
`urlparse(...).hostname`, which has none. A port-qualified entry therefore
compiles to a pattern that can never match, and every request is filtered as
offsite — silently. The crawl fetched the seed, reported success, and stopped.
I asserted the opposite before measuring it, on the strength of a helper
function that turned out not to be the one the middleware uses.

Both are the same lesson: **a framework's extension points are API surface you
do not control, and their failure mode is silence.** In minicrawl, seeding is a
line in a function I can read, and scope is `host_of(url) in seed_hosts`. That
is not better engineering — it is less engineering, with the tradeoff sitting
where I can see it.

## What this was for

Use Scrapy. It is more capable than this, better tested than this, and free.

But the settings above are not incantations any more. `ROBOTSTXT_OBEY` is
`robots.py`. `CONCURRENT_REQUESTS_PER_DOMAIN` is the lease in
`frontier/scheduling.py`. The dupefilter is the seen-set, and whether it holds
raw URLs or canonical ones decides whether `/a` is one page or ten. `HTTPCACHE`
is `freshness.py`, and the reason a cached response must still carry its
outlinks is that a 304 has no body.

And when a crawl misbehaves — 223 pages of a calendar, a site that asked for a
delay and did not get one, a spider that fetches exactly one page and reports
success — the shape of the problem is now recognisable, which is the only thing
the ladder was ever for.
