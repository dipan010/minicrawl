# Stage 13 — a front end, and what the real web did to it

Twelve stages were verified against a corpus this project wrote itself. That
was the point: ground truth you declare is ground truth you can diff against.
It is also the limitation, and this stage is where the bill arrives.

The front end is the vehicle. The findings are the substance.

## Why the crawl cannot run in the browser

The obvious design for "type a URL and watch it crawl" is a page that fetches
the URLs itself. It cannot work, and the reason is worth stating plainly
because it explains the shape of every crawler ever written:

> A browser will not let a page read another origin's response. Cross-origin
> reads are blocked unless the target site opts in with CORS headers, and no
> site on the web opts in for arbitrary readers. A page may *send* the
> request; it may not see what comes back.

So the crawl runs in Python and the browser is a **view** onto it. That is not
a compromise forced by this project's size — it is why crawlers are server
processes.

## Why there is no web framework

Two GET routes, no request bodies, no sessions, no templating. Flask, FastAPI
and aiohttp are each a larger install than the thing they would replace, and
this repo's core has exactly two dependencies. `asyncio.start_server` plus
about 120 lines of HTTP is honest at this size.

Streamlit was considered and rejected for the same reason plus two others: its
script-reruns-top-to-bottom model fights an async event stream, and its real
draw — public hosting on Community Cloud — is something a crawler must not
have. A public "crawl any URL" box sends traffic to arbitrary sites from a
shared host with your account attached. Local-only is a feature here.

## Server-sent events

The crawl streams, and the traffic goes one way, so SSE rather than a
WebSocket: an ordinary HTTP response that never ends, each message written as
`data: {json}\n\n`.

The blank line is the terminator. Omit it and the browser waits forever
without erroring — the same silent-failure shape as the two Scrapy hooks in
stage 10 that did nothing and reported success. This is why the SSE test
asserts the **bytes on the wire** rather than a parsed Python object, and why
the framing was checked with `curl -N` before any HTML existed.

The stream must also end with an explicit terminal event. A browser
`EventSource` treats a plain close as a dropped connection and reconnects —
which would silently start the crawl again.

`on_page` stays synchronous. `crawler._emit` calls it directly, and turning it
into a coroutine would change that contract for all twelve stages behind it,
so the callback does `queue.put_nowait` and the SSE route drains the queue.
The same split as `push`/`push_nowait` at stage 4, for the same reason: the
producer is not allowed to await.

## What the form does not offer

The CLI has `--ignore-robots`. The web form does not, and there is no
parameter that reaches `respect_robots`. A human running a CLI owns the
consequences of what they point it at; a form on a web page does not confer
that ownership on whoever loads it. `max_pages`, `workers` and a minimum
crawl delay are clamped server-side, because a limit the client can raise is
not a limit.

## Then it met real sites

Five sites, four pages each, robots respected, 1.5s delay:

| Site | Result |
|---|---|
| `en.wikipedia.org` | 4 pages, clean |
| `www.gov.uk` | 4 pages, clean |
| `httpbin.org` | frontier drained at 2 pages |
| `rfc-editor.org` | 4 pages, clean |
| `news.ycombinator.com` | 4 pages in **91.5 seconds** |

The Hacker News number looked like a bug and was the opposite. Its robots.txt
declares `Crawl-delay: 30`; three inter-request gaps at 30 seconds is 90, and
we spent 91.5. The crawler obeyed a real site's rate limit that the corpus
only ever declared at 0.2s. Stage 3's start-to-start delay accounting was
right, and this is the first evidence from outside the corpus.

`quotes.toscrape.com` produced the other pleasant surprise: three tag pages
flagged `near_duplicate` by stage 6's simhash. Tag listings genuinely are near
duplicates of one another — they differ by a handful of quotes in an otherwise
identical shell. That detector was tuned against two synthetic pages differing
by two words in 450, and it generalised.

## The bug the corpus could never have found

Every page in the corpus was UTF-8. So `extract.parse` handed bytes straight
to the parser, which assumes UTF-8, and nothing ever noticed. A page in
windows-1252 comes back as `Caf� na�ve`.

**It hid for twelve stages because link extraction survives it.** Hrefs are
ASCII, so a mangled page still yields the right links and the crawl looks
perfect. What is destroyed is everything downstream that reads text: titles,
word counts, and the shingles simhash computes over. A near-duplicate detector
comparing two corrupted strings still returns a number. Just not a true one.

That is the fourth appearance of this project's recurring theme, and the
sharpest: the failure was not merely invisible, it was *masked by a
neighbouring success*.

### Fixing the corpus first

Per the project rule — if the corpus tolerates a wrong implementation, the
corpus is wrong too — three pages were added before a line of the fix:

- `/encoded/latin1` — windows-1252 bytes, and an HTTP header that says so.
- `/encoded/mislabelled` — windows-1252 bytes, honest header, and a `<meta>`
  claiming UTF-8. Header and document disagree.
- `/encoded/undeclared` — windows-1252 bytes, **no charset on the header at
  all**, and the only declaration in the document is wrong. Every hint the
  standard offers is exhausted.

All three mojibake'd before the fix. Ground truth went from 21 expected pages
to 24, derived as always from `spec.py` rather than typed into the manifest.

### The precedence, and the step everyone skips

`minicrawl/charset.py`:

1. **BOM.** The encoding announcing itself in the bytes; it cannot be a stale
   copy-paste the way a meta tag can.
2. **HTTP `Content-Type` charset.** The server knows what it just encoded; the
   document only repeats what its author typed.
3. **`<meta charset>`**, scanned in the first 2 KB only — a declaration is
   required to appear early, and scanning whole documents for one is how a
   parser becomes the slow part of a crawl.
4. **UTF-8, strictly.**
5. **windows-1252**, which decodes every possible byte and therefore cannot
   fail.

Steps 2 and 3 decode **strictly**, and that is the whole design. Using
`errors="replace"` would honour a false declaration and destroy the text
silently; a strict failure is the only evidence that the page lied. The
recovery is not an edge case — `/encoded/undeclared` exists to make it a rule.

A trap avoided: `Fetched.content_type` has already had its parameters split
off, so it is `text/html` and never carries the charset. The header must come
from `fetched.headers["content-type"]`. Passing the convenient field would
have silently discarded the most reliable hint of the five.

The decision is recorded on `Extracted` as `encoding` and `encoding_source`,
because a correct answer and a lucky one are indistinguishable unless the
reason is written down.

Replay inherits the fix for free: the WARC kept the `Content-Type` header, so
an archived page decodes exactly as the live crawl did.

## What was deliberately not built

- **Statistical encoding detection.** When nothing declares anything and
  UTF-8 fails, the fallback is windows-1252 — mandated by the HTML standard,
  and wrong for most of the world. Shift-JIS or KOI8-R with no declaration
  decodes to plausible Latin nonsense rather than failing, because
  windows-1252 accepts every byte. Real crawlers add `chardet` or ICU to guess
  from byte frequencies.
  `test_characterises_no_encoding_detection_from_the_bytes_themselves` pins
  this, and fails when it lands.
- **`Retry-After` and 429 backoff.** Not encountered in these runs, but there
  is no backoff path, and a real crawl at volume will find one.
- **Resuming a stopped crawl.** Stop closes the event stream, which closes the
  generator, which cancels the crawl task — a viewer going away is a reason to
  stop making requests to someone else's site, not a reason to carry on
  quietly. But nothing is kept: pressing Crawl again starts over. The frontier
  can already persist to SQLite (stage 5) and the UI does not expose it.
- **Public hosting.** See above. This is a decision, not an omission.
