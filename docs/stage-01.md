# Stage 1 — one page, fetched and parsed

**Added:** `minicrawl/fetch.py`, `minicrawl/extract.py`
**Verify:** `uv run pytest tests/test_stage01_fetch_extract.py -q` (11 tests)

## What was actually hard

**The URL you asked for is not the URL you got.** After redirects, relative
links on the response must resolve against the *final* URL. `Fetched` carries
`url` and `final_url` separately for exactly this reason; collapsing them is a
bug that only shows up on redirecting sites.

**Read the body as a stream, cap it before it lands.** `/gen/1` is 64 KB and
links to more of itself. `client.stream()` plus a running byte count means a
hostile response costs you `max_bytes`, not your memory. The cap travels on the
`Fetched` record so `truncated` means something — comparing against a module
constant instead was the first bug this project produced.

**`<base href>` changes what every relative link on the page means.** `/docs/`
declares one; without honouring it, `one` resolves to `/one` instead of
`/docs/one` and a quarter of the corpus goes missing.

**One malformed href must not end the crawl — and dropping it takes real
work.** The corpus contains `http://127.0.0.1:8081:/a`. `urljoin` accepts it
without complaint; it only raises much later, when something reads its port.
`absolutise()` therefore *validates* — scheme, hostname, and a deliberate touch
of `.port` — and returns `None`. Resolving is not the same as validating, and
finding that out cost a failing test.

## Decisions worth remembering

- Errors are *data* (`Fetched.error`), not exceptions. A crawler that raises on
  a timeout has no way to record that a page was tried and failed.
- Content type is checked before parsing. A PDF is not HTML with weird tags.
