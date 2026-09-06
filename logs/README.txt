LOGS
====

One folder per stage: logs/stage-NN/notes.txt

These are working logs, not documentation. They record how each stage was
actually built and which concept each piece of code is an implementation of.

  docs/stage-NN.md   WHY the design is what it is. Narrative. Read for judgement.
  logs/stage-NN/     WHAT was done, in order, and WHICH CONCEPT each part maps
                     to. Procedural. Read for study or to retrace the work.

Every notes.txt uses the same sections:

  1. GOAL
  2. STEPS TAKEN            in the order they happened, including dead ends
  3. CONCEPTS APPLIED       the theory, stated independently of this codebase
  4. CONCEPT -> CODE MAP    which file/function is each concept made of
  5. WHAT BROKE             failures hit during the stage and what they taught
  6. HOW TO VERIFY          exact commands
  7. STATE AFTER THIS STAGE what works, what is still knowingly wrong

A stage is finished when its characterisation tests are flipped and the numbers
in section 6 reproduce.

THE STAGES
----------
  01  fetch one page, extract its links      HTTP, body caps, URL resolution
  02  the crawl loop                         frontier, BFS, cycles, scope
  03  robots.txt and politeness              RFC 9309, per-host rate limiting
  04  the worker pool                        concurrency without a DoS
  05  normalisation, traps, persistence      canonical URLs, shape budgets, resume
  06  content extraction and dedup           boilerplate, simhash, banding
  07  JavaScript rendering by exception      triage, not rendering
  08  freshness, sitemaps, priority          conditional GET, adaptive schedules
  09  distributed frontier                   atomic dedup, leases, shared clocks
  10  the framework comparison               what Scrapy buys, and what it does not
  11  storage                                content addressing, WARC, provenance
  12  CDX index and replay                   SURT, binary search on disk, offline
  13  front end, and the real web            SSE, CORS, character encodings
  14  exposing it safely                     SSRF, rate limits, fail-safe defaults

Where the WHAT BROKE sections are worth reading on their own: 05 (a
normalisation rule that invented 404s), 06 (a degenerate corpus that broke
simhash, and a finally block that recorded work never done), 08 (conditional
GET blinding the crawl), 10 (two framework hooks that failed in total silence),
11 (a corpus that could not expose the bug the stage was about), 12 (a binary
search that lost one key in two hundred while every spot check passed), and 13
(twelve stages of mojibake, hidden because link extraction never noticed), and
14 (a container that would not start, because an optional dependency had been
mandatory for five stages and every dev machine had it installed).
