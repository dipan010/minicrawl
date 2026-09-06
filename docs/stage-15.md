# Stage 15 — getting the crawl out

Stages 11 and 12 gave the crawl an archive with fidelity and an index. Neither
gave it an **exit**. A crawler that keeps everything and hands you nothing is a
crawler you cannot use for anything.

Three outputs, because three different readers want three different things.

## JSONL is the one that matters

One JSON object per line: URL, status, verdict, and the **clean text**.

This is the format everything downstream eats — an inverted index, an
embedding pipeline, a language model. It streams, so a crawl of any size costs
one line of memory to write and one line to read. And it is boring on purpose:
any program in any language can consume it without knowing this project exists.

The important detail is *which* text. The record carries `main_text` — the
boilerplate-stripped body from stage 6, the same text duplicate detection reads
— and not `text`, which includes the furniture. Exporting the raw body would
ship the navigation menu on every single page, and anything built from that
export would conclude that every page on the site is about the site's own menu.
That is not a hypothetical: it is the single most common way a scraped corpus
is quietly ruined.

## The HTML report has one hard requirement: no network

A downloaded report is read on a plane, in a meeting, from a USB stick. One
that fetches a webfont from Google renders wrong in exactly the place it is
most likely to be opened.

So `minicrawl/report.html` uses system fonts, inlines all its CSS and script,
and reaches out for nothing. The only external URL in the file is a link to the
repository in the footer — something to click, not something to load. A test
asserts that: it extracts every `src`/`href` beginning `http` and fails if any
of them is not that one link.

That constraint is why this report does not simply reuse `docs/index.html`,
which pulls Newsreader and IBM Plex from Google Fonts. Two files, because they
have different jobs: one lives on a web page that is always online, the other
is a file you carry away.

## The ZIP is a convenience with one sharp edge

`--bundle` collects the report, the text, the WARC and its index into one file.
`write_bundle` **skips members that do not exist**, so a crawl run without
`--warc` still bundles what it has.

That tolerance immediately caused a bug. The bundle was written before
`--cdx`, so the index did not exist yet, and the zip came out with three
members instead of four — silently, because skipping missing members is the
designed behaviour. Fixed by ordering: the bundle is written last, after every
output it might contain.

This is the same shape as the recurring theme, in miniature. A designed
tolerance for absence makes a genuine absence invisible.

## Where the text comes from, and why not the archive

Re-extracting from the WARC afterwards is possible — `replay.py` does exactly
that — and it would be the tidier story: export becomes replay with a different
output format.

It is also strictly worse here. It requires `--warc` to have been on, and it
re-parses every document a second time to recover something the crawler was
holding in its hand when it decided the page was worth keeping. So the exporter
takes the parse at the moment it exists, as a hook alongside the store and the
archive.

That required one small correction in the crawl loop. `found` was bound inside
`if got.is_html:`, so reaching it afterwards meant `locals().get("found")` —
which works and is the sort of thing that stops working silently. It is now
bound to `None` before the branch, because a PDF is still a crawled page and
everything downstream has to be able to ask for the parse and be told there
wasn't one.

## The 304 case, again

A page answered `304 Not Modified` has no body, therefore no text. It is
recorded anyway, with `text: null` and `from_cache: true`.

Dropping those pages would make a second crawl export fewer records than the
first while reporting the same page count — a discrepancy nobody would notice
until they diffed two exports and found documents missing. Stage 8 already
learned this lesson once when conditional GET blinded the crawl; the rule that
came out of it is that a 304 is a **third outcome**, not an error and not an
absence.

## Packaging, checked rather than assumed

Stage 14 shipped a container that would not start because an import was
optional in theory and mandatory in fact. So this stage does not assume the
HTML template ships with the code — it builds a wheel and asserts
`minicrawl/report.html` is inside it. It is, and now that is a fact rather than
a hope.

## What was deliberately not built

- **CSV.** Text with embedded newlines and quotes in a CSV is a format that
  works until someone opens it in a spreadsheet. JSONL has none of those edge
  cases, and `jq` converts it in one line if a spreadsheet is really wanted.
- **A download button in the web UI.** The streamed events do not carry page
  text — deliberately, since that would multiply the size of every message —
  so a browser-side download could only produce metadata. Offering a "download
  the contents" button that silently omits the contents is worse than not
  offering it. The CLI is the honest answer until the UI has somewhere to put
  the text.
- **Incremental/append export.** Each crawl writes its own file. Merging
  exports across crawls is a real want, and it is the same sorted-merge problem
  the CDX index already solved once — worth doing properly rather than by
  opening the file in append mode and hoping.
