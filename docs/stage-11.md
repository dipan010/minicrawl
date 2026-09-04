# Stage 11 — what the crawl leaves behind

**Added:** `minicrawl/store.py`, `minicrawl/warc.py`, `--store`, `--warc`
**Changed:** `fetch.py` keeps raw headers and the status line; the corpus gained `/compressed`
**Verify:** `uv run pytest tests/test_stage11_storage.py -q` (22 tests)

```
27 pages, 1 errors, 2 blocked by robots.txt — stopped: frontier drained
documents   19 unique, 2 exact dup, 3 near dup, 1 canonical alias, 2 refetched
stored      23 objects for 27 URLs — 208,902 bytes written, 1,764 deduplicated
warc        28 records, 21,837 bytes gzipped
```

Ten stages of crawler and not one byte kept. Everything before this fetched a
page, classified it, counted it and dropped it — fine for proving the crawl is
correct, useless for anything you would actually crawl *for*.

## "Store the pages" is three jobs wearing one name

| Job | Wants | Here |
|---|---|---|
| an archive | the response exactly as it arrived, replayable | `warc.py` |
| a corpus | one copy of each distinct document | `store.py` |
| an index | extracted text and metadata, queryable | `store.py` |

They pull in different directions. An archive keeps four copies of a page
served at four URLs, because *provenance is the point*. A corpus keeps one,
because *the document is the point*. Building one and calling it the other is
how you end up with an archive you cannot search or a corpus you cannot cite.

## Content addressing makes dedup stop being a feature

Name each object by the SHA-256 of its own bytes and deduplication becomes a
property of the naming scheme. Two URLs serving identical bytes write to the
same path; the second write is a no-op that costs a hash.

On this corpus that is **23 objects for 27 URLs**, and the groups are exactly
the ones earlier stages worked to identify:

```
4 URLs -> f422b2d0d1  ['/a', '/a/', '/a?a=1&b=2', '/r/1']
2 URLs -> 8d3ade3ecc  ['/dup/exact-1', '/dup/exact-2']
```

The four routes to `/a` are the ones stage 5 found by normalising and stage 6
found by hashing content — `/a` itself, the `/a/` that 301s, the query spelling
the server ignores, and the end of the `/r/1` redirect chain. Storage is where
identifying them finally *costs nothing*, because the store never had to be
told: it wrote them to the same filename.

Objects live at `<root>/objects/ab/abcdef...`. The two-character prefix
directory is not decoration — a flat directory of a million files is slow to
list on every filesystem worth naming, and the fan-out costs one string slice.

## Two things that must never be stored

Both look like nothing when they go wrong, which is this project's recurring
failure mode.

**A 304 has no body.** With `--freshness`, a second crawl is *mostly* 304s.
Let one reach the store and run two silently replaces every archived page with
zero bytes. `test_a_second_crawl_does_not_empty_the_store` crawls twice and
asserts that everything still round-trips — and that anything the server called
unchanged is byte-identical.

That test caught a real distinction on its first run: `/volatile` legitimately
differs between crawls, because it changes on every request. That is an update,
not an emptying, and the assertion had to say which it meant.

**A truncated body is not the document.** Storing it under a digest of the
fragment claims a completeness the bytes do not have. The default cap is 2 MB
and the largest corpus page is 64 KB, so this would never have fired by
accident — which is exactly why it would have shipped broken.

Both are refusals with a stated reason rather than exceptions, for the same
reason fetch errors are data: the crawl needs to record that it declined.

## The bug the corpus had to grow to expose

httpx decodes `Content-Encoding: gzip` transparently. So the body we hold is
the decoded entity while the headers still describe the compressed one, and
`Content-Length` is the *compressed* length. Write that pair into a WARC record
and every reader either rejects it or reads the wrong number of bytes.

The corpus served nothing compressed, so the case could not fire and the bug
would have shipped. `/compressed` was added first — a page served gzipped —
and only then was the writer built:

```
content-encoding header : gzip
content-length header   : 191 (bytes on the wire)
actual decoded body     : 255 bytes
```

A true archival crawler keeps the wire bytes and the headers stay honest. This
one does not have them — by the time `fetch` returns, the decoding has already
happened — so it reconciles in the other direction: drop `Content-Encoding`,
re-derive `Content-Length` from the body actually held, and record what it did
in an `X-Minicrawl-Decoded` field.

The record is then replayable and self-consistent, **but it is no longer
byte-exact provenance**: you cannot prove from the archive what the server put
on the wire. For a search corpus that is irrelevant. For evidence it is
disqualifying, and knowing which you are building is the whole decision.

## What `fetch.py` had to start keeping

An archive needs the status line and the headers as they arrived. The
lowercased `dict` every other stage uses has thrown away order and casing by
then, so `Fetched` now also carries `raw_headers`, `http_version` and
`reason_phrase`. Cheap to keep at fetch time, impossible to recover afterwards
— and a test asserts header order survives, because a normalising archive is a
lying archive.

## WARC, and why it is a pile of gzip members

The format is simpler than its reputation: a file is records concatenated, each
one a header block, a blank line, then a whole HTTP response. Each record is
gzipped as its *own member* and the members are concatenated. A gzip decoder
reads that as one stream; a tool that knows the trick seeks to any record
without decompressing what came before. That is the entire reason WARC files
are usable at a hundred gigabytes, and it is four lines of code.

`test_each_record_is_an_independent_gzip_member` walks the file with
`zlib.decompressobj(31)` and its `unused_data`, which is the canonical way to
prove members rather than a stream.

## The claim is verified by something that has never heard of us

Every WARC assertion is checked by reading the file back with **warcio**, an
independent library:

```
record types      : {'warcinfo': 1, 'response': 27}
length mismatches : none — every record is self-consistent
```

Self-asserting a file format is worth nothing. This is the same principle as
the manifest: the thing that judges the output must not share code with the
thing that produced it.

## Done-condition

There is no characterisation test left to flip, so the stage states its own:

- objects stored < URLs recorded, by the groups the corpus declares
- a second crawl over a freshness store empties nothing
- `warcio` reads every record, and every record declares the length it holds
