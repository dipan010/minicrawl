# Stage 12 — CDX index and replay

Stage 11 could write an archive. It could not read one. That is a backup
nobody has ever restored: the format was validated, the *use* was not.

Two pieces close the loop. A CDX index says where a record is. Replay reads it
back and runs the crawler's own parser over it with the origin switched off.

## The index is a sorted text file, and that is the design

The obvious index is a hash table — URL to offset — pickled to disk. It is
faster to write, simpler to explain, and wrong at the size that matters:
loading it costs the memory of the whole index before answering one question,
and it cannot answer "everything under example.com" at all.

Web archives use a sorted flat file instead, because sorting buys two things a
hash gives up:

- **Binary search without loading.** Seek to a byte midpoint, scan to the next
  newline, compare, recurse on a half. Memory is one line, no matter whether
  the file is 6 KB or 40 GB.
- **Range queries.** Everything under a host is one contiguous run, answered
  by a seek and a forward scan.

Both depend on the key. `SURT` reverses the host labels, so `www.example.com`
becomes `com,example,www)` and every URL under `example.com` — subdomains
included — lands in one span of the file.

### What SURT does with this corpus

The corpus is served from `127.0.0.1:8081`, an IP with a port. Reversing
`127.0.0.1` to `1,0,0,127` would sort an address beside unrelated addresses
that happen to share a last octet, and would imply a hierarchy an IP does not
have. So IP literals are left alone, and the port stays in the key — without
it, ports 8081 and 8082 would collide into one key and the four corpus hosts
would become one.

This is the same shape as the problem stage 9 hit with Scrapy's
`allowed_domains`: the interesting case is the one the corpus actually has,
not the one the textbook example uses.

## The binary search had a bug the "does it work" test could not see

The first implementation advanced the low bound to wherever the read had left
the handle, rather than to `middle + 1`. It found the right record for every
key that was spot-checked. It lost roughly one key in two hundred — including
`p00004` — because moving `low` past the line just examined can step over the
target when the target is the very next line.

Two tests caught it, and neither is the obvious one:

- **`test_every_url_in_the_index_is_findable`** looks up all 1000 keys. A
  subtly wrong search finds most of them, so checking a few proves nothing.
- **`test_lookup_reads_a_handful_of_lines_not_the_file`** asserts fewer than
  30 lines are read from a 4000-line index. A linear scan returns the right
  answer too; what separates a binary search from a scan is how much of the
  file it touched. Without this test, "binary search" is an unverified claim
  about code that happens to return the right result.

## The offset is only meaningful because of stage 11's framing

Stage 11 wrote each record as its own gzip member and justified it with a
seekability argument that nothing exercised. This stage spends it: the bytes
between `offset` and `offset + length` are a complete gzip stream, so they
decompress alone, with nothing before them read. Replaying 27 records touched
21,508 bytes of a 21,822-byte archive here — the archive is small, so nearly
all of it was wanted; the point is that the *unwanted* part was never read.

Getting the offset out required straightening `WarcWriter`. `_write_record`
had a special case picking `self._handle` for warcinfo and `self._open()` for
everything else, to dodge a reentrancy that does not exist: `_open` assigns
`_handle` *before* writing warcinfo, so the nested call is a no-op. The
special case went, and `_write_record` now returns `(offset, length)`.

The index is collected in the writer, at write time, because that is the only
place that knows the offset. Building it in a second pass would mean
re-reading the whole archive to recover something we held and threw away.

## Validated against a tool that has never heard of this project

`warcio index` reports offsets and lengths for a WARC. Every response record's
pair is compared against it, not a spot check — an error in the warcinfo
record's own offset shifts nothing after it, so checking only the first hit
would pass while the file was wrong.

## Replay, with the server dead

The headline test crawls the live corpus, then poisons `socket.connect` and
`socket.create_connection` and re-derives every page's outlink count from the
archive. Killing the server would be a weaker test — a stale server on the
same port would let it pass by accident. Poisoning the socket cannot be
satisfied by anything but genuinely not using the network.

Two wrinkles this project built for itself:

- **`/compressed`** was archived *decoded*, with `Content-Encoding` stripped
  and `Content-Length` re-derived. A reader that gunzips on sight corrupts it.
  The record admits what happened in `X-Minicrawl-Decoded`; the correct
  behaviour is to trust the framing, not to sniff the body.
- **`/volatile`** changes on every request, so an archived copy can never
  equal a fresh fetch. It is excluded *by construction* — the comparison is
  between the live crawl and the archive **of that same crawl**, one capture
  compared with itself — rather than by an exception list added after a test
  went red.

Record bodies are read by `Content-Length`, never by scanning for the
terminating blank line, because a body may contain one. There is a test with a
blank line in the body that fails if that is ever "simplified".

## What was deliberately not built

- **Replay as a fetcher.** `ArchiveReplay` hands archived bytes to
  `extract.parse` and stops. The moment it grows a robots check, a politeness
  wait, or a retry, it becomes a second fetch path and the archive stops being
  the thing under test.
- **A CDX server.** The wire protocol (`/cdx?url=...&matchType=prefix`) is a
  web framework exercise, not a crawling one.
- **Compressed indexes (ZipNum).** Real archives gzip the index in blocks with
  a second-level index over it. It is the same idea applied twice, and adding
  it would obscure the idea being taught.
