# The corpus

Four origins (`127.0.0.1:8081`–`8084`) serving the same declared graph.
Politeness keys on `host:port`, so these count as four hosts — without that,
per-host queues (stage 4) and host sharding (stage 9) cannot be demonstrated
at all.

## Pathologies, and the stage each one targets

| In the corpus | Why it is there | Stage |
|---|---|---|
| `<base href>` on `/docs/`, protocol-relative and bare-relative hrefs | URL resolution is not string concatenation | 1 |
| `http://127.0.0.1:8081:/a` (unparseable) | one bad href must not kill the crawl | 1 |
| `/gen/<n>` — unbounded chain of 64 KB pages | body caps and depth caps, not optimism | 1, 5 |
| `/hang` (30 s) and `/slow` (0.8 s) | per-request timeouts; slow ≠ dead | 1, 4 |
| `/r/1 → /r/2 → /r/3 → /a` | the fetched URL is not the final URL | 1 |
| `/loop/1 ⇄ /loop/2` | redirect loops terminate as errors | 1 |
| robots 404 on :8082, **500 on :8083** | RFC 9309: 404 allows all, 5xx **disallows all** | 3 |
| `Allow: /private/public-corner` under `Disallow: /private/` | longest-match wins, not first-match | 3 |
| `Crawl-delay: 0.2` | politeness is a per-host clock | 3, 4 |
| 14 spellings of `/a` on `/variants` | trailing slash, default port, dot segments, param order, `utm_*`, session ids | 5 |
| `/dup/exact-{1,2}` | identical text — caught by a content hash | 6 |
| `/dup/near-{1,2}` | 450 words, **2 edited** — caught by simhash at distance 4 | 6 |
| `/gen/*` filler is varied prose | one repeated phrase gives 4 distinct shingles and breaks simhash outright | 6 |
| `/dup/canonical-source` with `rel=canonical` → `/a` | the page tells you it is an alias | 6 |
| `/js-only` whose only link appears after JS runs | you cannot detect this from the HTTP response alone | 7 |
| `/etag` with real 304s | conditional GET is how recrawling stays cheap | 8 |
| `sitemap.xml` as an *index* of two sitemaps | seeds do not have to come from crawling | 8 |
| `/files/*.pdf`, `*.png` | content-type gating before parsing | 1 |

## Regenerating ground truth

```bash
uv run python -m testsite.manifest > testsite/manifest.json
```

Edit `testsite/spec.py`, never `manifest.json`.
