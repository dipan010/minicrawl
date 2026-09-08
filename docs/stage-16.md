# Stage 16 — reader mode

One URL in, clean Markdown out. This is the shape an LLM agent actually calls,
and it is not a crawl.

## The difference is politeness, not optimisation

> **A crawl discovers.** It follows links, so it needs a frontier, a seen-set,
> trap budgets and depth limits, and it visits one host many times — which is
> why politeness dominates its clock. A polite crawl is mostly waiting, by
> design.
>
> **A read is told exactly what to fetch.** No frontier, no discovery, usually
> one page per host, so the per-host queue that makes crawling slow never
> engages. Latency is one round trip plus a parse.

Same fetcher, same parser, opposite tuning. Measured over the same pages
(`scripts/reader_bench.py`):

| | local corpus, 15 URLs | real sites, 8 URLs |
|---|---|---|
| read, robots checked | **0.06 s** wall, p50 9.6 ms | **1.88 s** wall, p50 1062 ms |
| read, robots skipped | 0.04 s wall, p50 3.7 ms | 1.32 s wall, p50 950 ms |
| read, one at a time | 0.01 s wall | 4.86 s wall |
| crawl, same page count | 3.62 s wall | 8.10 s wall |

The crawl spent 22 and 34 worker-seconds waiting respectively. That is
politeness, not overhead, and it is the entire gap. Reader mode is fast
because it is **doing less**, not because anything was tuned — and saying so is
the honest version of "lightning fast".

The benchmark reports robots checked and robots skipped separately rather than
quoting whichever number flatters the result. On real sites the check costs
about 0.5 s across eight URLs, because `robots.txt` is fetched once per host
and cached; a read of fifty URLs over three hosts pays for three files.

## Markdown, not text

`main_text` already exists and flattens a page to a string for duplicate
detection. It is the wrong output here.

The consumer is a language model, and **structure is most of what tells a
reader which sentence is the claim and which is the caveat.** A heading has to
stay a heading, a list a list, a code block a code block. So
`minicrawl/markdown.py` is a recursive walk that keeps headings, lists (nested,
indented), code fences, block quotes, tables with separator rows, emphasis, and
links — made absolute, because a relative link in extracted Markdown looks
usable and is not, which is worse than omitting it.

No dependency. `markdownify` and `html2text` both exist and both do more than
this needs; writing the walk is the only way to know what it does with a nested
list.

### The allowlist is inline, and that is the design

The first version listed *block* tags and treated everything else as inline.
Handed `<article>`, which was not on the list, it flattened an entire document
into one run-on paragraph.

Block-level containers are open-ended — `<article>`, `<section>`, a custom
element — while inline elements are a small closed set. So the test is
inverted: **unknown tag means container, not inline.** A test uses a
`<my-block>` element to keep it that way.

### The bug that produced no visible damage

`_inline(node)` formatted a node's *children*. Called on a `<strong>` directly
— which is what a block walker does — it emitted the text without the `**`.

Nothing looked broken. The words were all present, correctly ordered, properly
spaced. Only the emphasis, the links and the inline code were silently gone,
which for a model reading the output is exactly the information it needed.

The fix is `_inline_node`, which formats one node *including its own tag*, with
`_inline` iterating children over it. There is a test per inline element rather
than one for prose, because prose looks fine either way.

## robots.txt is still checked

A reader could skip it, and most commercial ones do, on the argument that a URL
a human pasted is a URL a human could have opened in a browser.

This one checks by default and lets the CLI opt out with `--ignore-robots`,
matching the rule the rest of the project follows: the person running a command
owns what it does; a form on a web page does not.

`RobotsGate` fetches once per host, under a per-host lock — fifty concurrent
reads of one host must produce **one** `robots.txt` request, not fifty racing to
fill the same cache slot.

## The endpoint that quietly did not obey it

`read_one` defaults `robots=None`, which is right for a library call the caller
controls. `GET /read` forgot to pass a gate, and served a disallowed page with
a `200`.

No error, no warning — a rule simply not applied. Stage 14's whole claim is
that a hosted instance always obeys robots and cannot be switched off from a
form, and a smaller endpoint does not get an exemption. Now fixed, and pinned
by a test that asserts a `502` and the reason.

This is the same shape as everything else this project has found: **the failure
was the absence of an argument**, and absence is invisible.

## What was deliberately not built

- **HTTP/2.** It needs the `h2` package, and the core install is two
  dependencies. Worth measuring before adding, not assuming.
- **A cache.** Reading the same URL twice refetches it. `freshness.py` already
  knows how to do conditional GET; wiring it in is a real want and belongs with
  the index work, where repeated reads actually happen.
- **Screenshot or PDF rendering.** `render.py` can drive Playwright, and
  reader mode does not use it. Triage first would be the right design, and
  nothing here has needed it yet.
- **Concurrency per host.** `read_many` limits total concurrency with a
  semaphore, not per host. Point it at fifty URLs on one host and it behaves
  like a small load test. That is what the crawler is for, and the docstring
  says so rather than pretending the two are interchangeable.
