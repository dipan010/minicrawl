# Stage 3 — robots.txt and politeness

**Added:** `minicrawl/robots.py`, `minicrawl/politeness.py`, `RobotsCache` in `crawler.py`
**Verify:** `uv run pytest tests/test_stage03_robots.py -q` (22 tests)
**Flipped:** `test_characterises_no_robots_support_yet` → `test_disallowed_pages_are_never_fetched`

## Why the parser is hand-rolled

`urllib.robotparser` ships with Python and using it would have skipped the
lesson entirely. It also gets this corpus wrong three ways: no `$` anchoring,
non-RFC longest-match handling, and — the important one — it treats a 500 as
permission to crawl.

## The four rules that carry the weight

**A 404 and a 500 mean opposite things.** RFC 9309 §2.3.1.3: 4xx means the site
published no rules, so crawl freely. §2.3.1.4: 5xx or unreachable means assume a
**complete disallow**. This is the rule most implementations invert, and the
reasoning is worth internalising — a site that is failing is not a site that is
granting consent. The corpus serves a 404 on `:8082` and a 500 on `:8083` so
that getting this backwards is a test failure, not a production incident.

**Longest match wins, not first match.** `Allow: /private/public-corner` beats
`Disallow: /private/` because it is a longer pattern, regardless of line order.
On an exact tie, `Allow` wins. Ordering-based parsers pass most robots files and
then quietly do the wrong thing on the ones that matter.

**The most specific User-agent group wins, and only that group applies.** Rules
do not merge. `:8081` disallows `/files/` for `minicrawl` but not for anyone
else, and disallows `/*.pdf$` for everyone else but does not repeat it in our
group. Both facts are asserted.

**`*` and `$` are the only metacharacters.** Everything else is escaped —
`compile_pattern` builds the regex character by character rather than trusting
the pattern, because a robots.txt is attacker-controlled input.

## Politeness is per host, and that is the whole point

Ten hosts at one request per second each is polite. One host at ten requests per
second is an outage. `Politeness` keys its clock on `host:port` — which is
exactly why the corpus runs on four origins. Against a single-origin test site,
a correct per-host implementation and a global `sleep()` are indistinguishable,
and you would not find out until stage 4 made it matter.

Two details that are easy to get wrong:

- **The robots.txt fetch is exempt from Crawl-delay.** You cannot honour a delay
  you have not read yet.
- **Crawl-delay is the gap between request *starts*.** Fetch time counts toward
  it, so total sleep is always slightly under `delay × gaps`. The test asserts
  against the wall clock, not the sleep total — the first version of that test
  failed by 14 ms for exactly this reason.

A hostile `Crawl-delay: 86400` is clamped by `max_delay`. Politeness is a
courtesy the crawler extends, not a lever the site gets to pull without limit.

## The result

```
blocked by robots : ['/files/report.pdf', '/private/secret']
polite sleep      : 7.86s over 40 pages     # 0.2s crawl-delay, honoured
sitemaps found    : ['http://127.0.0.1:8081/sitemap.xml']

  :8082  robots 404 -> allow all      pages=25  blocked=0  gen=1
  :8083  robots 500 -> disallow all   pages= 0  blocked=1  gen=0
  :8084  Disallow: /gen/              pages=25  blocked=1  gen=0
```

`:8083` crawling nothing at all is the headline. So is `:8084`, where the
generator trap is closed by robots.txt alone — on `:8081` it is still wide open,
because nothing in this stage recognises a trap. That is stage 5.

The `--verify` diff still reads `0 missing, 13 extra`, but the extras have
changed character entirely: they are now **only** `/gen/*` plus the
unnormalised `/a/`. The two robots-disallowed pages are gone, and the crawl
simply spends the freed `max_pages` budget going deeper into the trap.

## Sitemaps

`Sitemap:` directives are collected but not yet followed — stage 8 uses them as
a second source of seeds, which is how you find pages nothing links to.
