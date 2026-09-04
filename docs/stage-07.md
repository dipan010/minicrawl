# Stage 7 — rendering the pages that need it, and only those

**Added:** `minicrawl/render.py`, triage signals in `extract.py`, escalation in the crawl loop
**Verify:** `uv run pytest tests/test_stage07_render.py -q` (15 tests, 3 need Chromium)
**Install:** `uv sync --extra render && uv run playwright install chromium`

```
                      wall     pages  rendered  browser time   /js-only/child
no browser            1.03s      25         0        0.00s     not found
triage (this stage)   1.80s      26         1        0.78s     found
render everything    16.64s      26        26       15.49s     found
```

Both browser runs find exactly the same pages. One of them pays **twenty times
the browser cost** to do it.

## The stage is not about rendering

Driving a headless browser is a solved problem and about forty lines. The
interesting work is the **triage**: deciding, from the response you already
have, whether a browser would tell you anything new.

Get it wrong in one direction and you miss content. Wrong in the other and the
crawl costs an order of magnitude more than it should — 9× wall clock on this
corpus, and that is with a local server and no network latency to amortise.

## The signals, and why none of them stands alone

All four are computed from markup already parsed, so triage is free.

| Rule | Reasoning |
|---|---|
| `empty_app_root` | An empty `#app` / `#root` / `app-root` is the markup literally saying "something will be inserted here". Nearly conclusive. |
| `noscript_hint` | A `<noscript>` mentioning JavaScript is the page telling you itself. Rare, cheap, unambiguous. |
| `thin_and_script_heavy` | Few words **and** scripts outweighing prose. Neither half is sufficient. |
| `no_links_with_scripts` | Text but no links at all, with scripts present — navigation that has not been built yet. |

The conjunctions are the load-bearing part. `/docs/one` is **three words** and
needs no browser; a rule firing on length alone escalates most of a
documentation site. A documentation page can be script-heavy and still fully
rendered server-side; a rule firing on script weight alone escalates the rest.

Triage reports **reasons, not a score**. When a crawl escalates the wrong page
you need to know which rule fired, and an opaque number cannot tell you.

## An ordering constraint, inherited from stage 6

`main_text()` strips `<script>`. The script signal has to be read *before* that
happens, so the triage inputs are computed inside `extract.parse()` alongside
the links, not derived afterwards from `Extracted`. A test pins it: reversing
the order would silently make `script_bytes` zero and quietly disable two of the
four rules.

## Escalation is a fallback, never a dependency

`PlaywrightRenderer.render()` returns `None` on any failure and the crawl
continues with the unrendered response — which is the result we would have had
anyway. A browser that dies is an inconvenience, not a crawl failure.

One browser is launched for the whole crawl. Launching per page costs about a
second and would make the escalation dwarf the fetch it is supplementing.

## Politeness survives without new machinery

Rendering happens while the worker still holds that host's in-flight slot, so
a render's subresource requests cannot overlap another crawl request to the
same origin. Escalation was designed to sit inside the request, not beside it,
and stage 4's per-host serialisation covers it for free.

## What this buys on the corpus

`/js-only/child` has exactly one inbound link and JavaScript writes it. No
amount of HTTP-level cleverness reaches it — the control test asserts that the
non-browser crawl cannot, which is what makes the browser crawl finding it
mean something.

The manifest gained `expected_pages_rendered`: the same 19 pages plus that
one. (The corpus has grown since: `/volatile` at stage 8 and `/compressed`
at stage 11, so at `HEAD` these read 21 and 22.)
Ground truth now describes two crawl configurations, and each is asserted
against the one that applies to it.

## Cost note

`playwright_available()` checks the filesystem rather than asking Playwright.
Asking means starting its driver process — about a second, plus a noisy
teardown warning — which is far too much for a question that only gates a test
skip.
