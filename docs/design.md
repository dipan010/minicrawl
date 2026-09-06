# minicrawl — high- and low-level design

Two views of the same system. The **HLD** is what the pieces are and why the
boundaries fall where they do. The **LLD** is how one URL actually travels
through them, and what each module is responsible for.

Everything here describes code in this repository. Where a design has a known
gap, it says so rather than drawing the version that would look better.

---

# Part 1 — High-level design

## 1.1 The shape of the system

A crawler is a loop with a queue in the middle. Everything else is a defence
against the ways that loop goes wrong: the same page under fourteen names, a
server that generates pages forever, a site that asks you to slow down, a
document you already have.

```mermaid
flowchart LR
  seeds([Seed URLs]):::io --> FR

  subgraph core["The loop"]
    direction LR
    FR[["Frontier<br/><i>what to fetch next</i>"]]:::core
    W(["Worker pool<br/><i>N coroutines</i>"]):::core
    FE["Fetch<br/><i>httpx, byte cap</i>"]:::core
    EX["Extract<br/><i>decode, parse, links</i>"]:::core
    FR -->|acquire| W --> FE --> EX
    EX -->|new links| FR
  end

  subgraph gates["Gates — asked before a request is made"]
    RO["robots.txt<br/>RFC 9309"]:::gate
    PO["Politeness<br/>per host:port"]:::gate
    TR["Trap guard<br/>URL shape budgets"]:::gate
    NO["Normalise<br/>RFC 3986"]:::gate
  end

  subgraph after["Judgement — after the body arrives"]
    DE["Dedup<br/>SHA-256 · simhash"]:::after
    RE["Render triage<br/>reasons, not a score"]:::after
    FRESH["Freshness<br/>ETag · adaptive"]:::after
  end

  subgraph out["Outputs"]
    ST[("Content store<br/>content-addressed")]:::io
    WA[("WARC 1.1<br/>gzip member each")]:::io
    CX[("CDX index<br/>sorted, SURT")]:::io
  end

  W -.consults.-> RO & PO & TR
  EX -.-> NO -.-> FR
  EX --> DE & RE
  FE <-.-> FRESH
  EX --> ST & WA
  WA --> CX
  CX --> RP["Replay<br/><i>offline re-extraction</i>"]:::after

  classDef core fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef gate fill:#b07a16,stroke:#7d5610,color:#fff
  classDef after fill:#2f4858,stroke:#1d2d38,color:#fff
  classDef io fill:#efece4,stroke:#c9c5ba,color:#1a1a17
```

**Why the boundaries are here.** Three of them do real work:

| Boundary | What it buys |
|---|---|
| **Frontier is an interface, not a queue** | The same crawl runs on a deque, a heap, SQLite or Redis. Persistence and distribution become *storage* choices rather than rewrites. |
| **Gates run before the request** | robots, politeness and trap budgets can only protect you if they are consulted before bytes move. A check after the fetch is an audit, not a defence. |
| **Outputs are downstream of extraction** | The store, the archive and the index never influence the crawl. A crawl with no store behaves identically to one with a store — the `NullStore` exists to make that literal. |

## 1.2 Concurrency model

One event loop, N worker coroutines, and exactly **one in-flight request per
host**. That last constraint is the whole politeness design: it is enforced by
the frontier handing out work, not by workers agreeing to behave.

```mermaid
flowchart TB
  subgraph loop["asyncio event loop — one thread"]
    W1(["worker 1"]):::w
    W2(["worker 2"]):::w
    W3(["worker 3"]):::w
    Wn(["worker N"]):::w
  end

  W1 & W2 & W3 & Wn <-->|"acquire() / release()"| F

  F[["SchedulingFrontier<br/><b>asyncio.Condition</b><br/>picks a host that is idle AND due"]]:::f

  F --> H1["host A<br/>queue · next-allowed clock"]:::h
  F --> H2["host B<br/>queue · next-allowed clock"]:::h
  F --> H3["host C<br/>queue · next-allowed clock"]:::h

  classDef w fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef f fill:#2f4858,stroke:#1d2d38,color:#fff
  classDef h fill:#efece4,stroke:#c9c5ba,color:#1a1a17
```

A worker never sleeps for politeness; it waits on a condition. The frontier
wakes it when some host is both idle and past its next-allowed time. So eight
workers on one host produce a **sequential, correctly spaced** crawl rather
than eight simultaneous requests, and the same eight workers across four hosts
run four at a time.

`Crawl-delay` is measured **start to start**, not slept after each fetch — the
fetch time counts toward the interval, which is why Hacker News (`Crawl-delay:
30`) takes ~90s for four pages and not ~120s.

## 1.3 Deployment views

The same crawler core is wrapped four ways. Only the wrapper changes.

```mermaid
flowchart TB
  CORE{{"minicrawl core<br/><i>httpx + selectolax, nothing else</i>"}}:::core

  CLI["<b>CLI</b><br/>uv run minicrawl<br/><i>every flag, incl. --ignore-robots</i>"]:::v
  WEB["<b>Local web UI</b><br/>python -m minicrawl.web<br/><i>SSE, LOCAL policy</i>"]:::v
  DOCK["<b>Container</b><br/>Dockerfile / render.yaml<br/><i>PUBLIC policy: allowlist, SSRF guard</i>"]:::v
  PAGES["<b>GitHub Pages</b><br/>static files<br/><i>replays recorded crawls</i>"]:::v

  CORE --> CLI & WEB & DOCK
  CORE -.->|"recorded ahead of time<br/>by scripts/record_crawls.py"| PAGES

  CLI --- N1["a human owns<br/>the consequences"]:::n
  DOCK --- N2["a stranger picks<br/>the target"]:::n
  PAGES --- N3["no server exists<br/>to pick a target"]:::n

  classDef core fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef v fill:#2f4858,stroke:#1d2d38,color:#fff
  classDef n fill:#efece4,stroke:#c9c5ba,color:#1a1a17,font-size:11px
```

The asymmetry is deliberate and is the subject of stage 14: **limits belong to
exposure, not to the crawler.** The CLI keeps `--ignore-robots` and can crawl
loopback. A public container may do neither, because the person choosing the
target is no longer the person answering for it.

## 1.4 How correctness is established

Nothing in this project is verified against the crawler's own output.

```mermaid
flowchart LR
  SPEC["<b>testsite/spec.py</b><br/>the corpus declared as data:<br/>pages, redirects, traps, duplicates"]:::src
  SERVER["<b>testsite/server.py</b><br/>serves that declaration<br/>over HTTP"]:::mid
  MAN["<b>testsite/manifest.py</b><br/>derives expectations<br/><i>no HTTP, no HTML parsing</i>"]:::mid
  JSON[("manifest.json<br/><i>ground truth</i>")]:::truth
  CRAWL["a crawl"]:::mid
  DIFF{"diff"}:::gate

  SPEC --> SERVER --> CRAWL --> DIFF
  SPEC --> MAN --> JSON --> DIFF
  DIFF -->|"equal"| OK["correct"]:::ok
  DIFF -->|"differs"| BAD["a bug — in the crawler<br/><b>or in the corpus</b>"]:::bad

  classDef src fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef mid fill:#2f4858,stroke:#1d2d38,color:#fff
  classDef truth fill:#b07a16,stroke:#7d5610,color:#fff
  classDef gate fill:#efece4,stroke:#c9c5ba,color:#1a1a17
  classDef ok fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef bad fill:#a33224,stroke:#77241a,color:#fff
```

The two paths from `spec.py` never meet until the diff. The manifest is
**derived**, never written by hand, and the rule is that a crawl is never made
to pass by editing ground truth — if the corpus tolerates a wrong
implementation, the corpus is wrong too. That has been acted on four times
(stages 6, 8, 11 and 13).

---

# Part 2 — Low-level design

## 2.1 The journey of one URL

This is the hot path, in order, with the decision points that can end it early.

```mermaid
sequenceDiagram
  autonumber
  participant W as Worker
  participant F as Frontier
  participant R as RobotsCache
  participant P as Politeness
  participant H as Origin server
  participant X as extract
  participant D as DuplicateIndex
  participant S as Store / WARC

  W->>F: acquire()
  F-->>W: Request(url, depth, priority)
  Note over F: only if the host is idle<br/>and past its next-allowed time

  W->>R: allows(url)?
  alt disallowed
    R-->>W: no
    W->>F: release(done=True)
    Note right of W: counted as blocked_by_robots,<br/>never fetched
  else allowed
    R-->>W: yes + crawl_delay
    W->>P: wait_turn(host)
    W->>H: GET (If-None-Match if known)
    alt 304 Not Modified
      H-->>W: 304, no body
      Note over W: a third outcome, not an error.<br/>Links come from the freshness store,<br/>or the crawl goes blind.
    else 200
      H-->>W: status, headers, body (byte-capped)
      W->>X: parse(body, url, content-type)
      X-->>W: title, links, main_text, encoding
      W->>D: classify(main_text, canonical)
      D-->>W: new | exact | near | canonical_alias | already_seen
      W->>S: put(fetched) / write_response(fetched)
      S-->>W: digest, (offset, length)
    end
    W->>F: push(normalised links, depth+1)
    W->>F: release(done=True)
  end
```

Two details that are easy to get wrong and are load-bearing here:

- **`release(done=...)` is not a formality.** Hitting `max_pages` while holding
  a request must release it as *unfinished*. Marking it done wrote "completed"
  to SQLite for a URL never fetched, and every resumed crawl then skipped it —
  a stage-5 bug found at stage 6.
- **A 304 has no body, so it has no links.** Caching without keeping the
  outlinks turned a 27-page crawl into a 10-page one. The freshness store
  therefore persists links, not just validators.

## 2.2 Module responsibilities

```mermaid
flowchart TB
  subgraph L1["Entry points"]
    cli["cli.py"]:::e
    web["web/server.py<br/>web/policy.py"]:::e
  end
  subgraph L2["Orchestration"]
    crawler["crawler.py<br/><i>CrawlConfig · crawl() · Page · CrawlResult</i>"]:::o
  end
  subgraph L3["Per-request"]
    fetch["fetch.py<br/><i>Fetched, byte cap</i>"]:::m
    robots["robots.py<br/><i>RobotsTxt, longest match</i>"]:::m
    polite["politeness.py"]:::m
    render["render.py<br/><i>triage → Playwright</i>"]:::m
  end
  subgraph L4["Per-document"]
    charset["charset.py<br/><i>BOM→header→meta→utf8→cp1252</i>"]:::m
    extract["extract.py"]:::m
    normalize["normalize.py<br/><i>+ url_shape</i>"]:::m
    dedup["dedup.py<br/><i>simhash, banding</i>"]:::m
    traps["traps.py"]:::m
  end
  subgraph L5["Scheduling"]
    frontier["frontier/*<br/><i>4 backends, 1 interface</i>"]:::s
    fresh["freshness.py"]:::s
    sitemap["sitemap.py"]:::s
  end
  subgraph L6["Persistence"]
    store["store.py"]:::p
    warc["warc.py"]:::p
    cdx["cdx.py"]:::p
    replay["replay.py"]:::p
  end

  cli & web --> crawler
  crawler --> fetch & robots & polite & render
  crawler --> frontier & fresh & sitemap
  fetch --> extract
  extract --> charset
  extract --> normalize --> traps
  extract --> dedup
  crawler --> store & warc
  warc --> cdx --> replay
  replay --> extract

  classDef e fill:#b07a16,stroke:#7d5610,color:#fff
  classDef o fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef m fill:#2f4858,stroke:#1d2d38,color:#fff
  classDef s fill:#3d5a6c,stroke:#263945,color:#fff
  classDef p fill:#efece4,stroke:#c9c5ba,color:#1a1a17
```

`replay.py → extract.py` closes the loop: an archived page is re-parsed by the
**same** parser the live crawl used, which is what makes the archive's fidelity
testable rather than asserted.

## 2.3 The frontier: one interface, four storages

```mermaid
classDiagram
  class Frontier {
    <<Protocol>>
    +push(Request)
    +acquire() Request
    +release(url, done)
    +known(url) bool
  }
  class SchedulingFrontier {
    <<abstract>>
    -Condition _cv
    +acquire() Request
    +release(url, done)
    #_add(Request)*
    #_take(host)*
    #_hosts_with_work()*
    #_complete(url)*
    #_requeue(url)*
    #_mark_seen(url)*
    #known(url)*
  }
  class MemoryFrontier {
    deque per_host
  }
  class PriorityQueue {
    heapq per_host
  }
  class SqliteFrontier {
    table resumable_rows
  }
  class RedisFrontier {
    SADD atomic_dedup
    SETNXPX worker_leases
  }

  Frontier <|.. SchedulingFrontier
  SchedulingFrontier <|-- MemoryFrontier
  SchedulingFrontier <|-- PriorityQueue
  SchedulingFrontier <|-- SqliteFrontier
  SchedulingFrontier <|-- RedisFrontier
```

The base class owns **all** the scheduling — host readiness, the condition
variable, the one-request-per-host rule. A subclass only answers storage
questions: put this somewhere, take one for this host, which hosts have work,
mark this done. Adding a backend is six small methods, and none of them can get
politeness wrong because none of them are asked about it.

## 2.4 The lifecycle of one URL in the frontier

```mermaid
stateDiagram-v2
  [*] --> Unknown
  Unknown --> Queued: push() — normalised, trap-budgeted, unseen
  Unknown --> Seen: mark_seen() — a redirect landed here
  Queued --> Leased: acquire() — host idle and due
  Leased --> Done: release(done=True)
  Leased --> Queued: release(done=False) — max_pages hit while held
  Leased --> Queued: lease expired — a worker died holding it
  Done --> [*]
  Seen --> [*]
```

The two transitions back to *Queued* are both bugs that were shipped once. A
missing requeue is invisible: the crawl finishes, reports success, and is
quietly short a page. Only diffing against declared ground truth found either.

## 2.5 What persistence stores, and how the parts refer to each other

```mermaid
erDiagram
  RESPONSE ||--|| WARC_RECORD : "archived as"
  WARC_RECORD ||--|| CDX_LINE : "indexed by"
  RESPONSE }o--|| OBJECT : "addressed by SHA-256"
  URL }o--|| RESPONSE : "fetched into"

  OBJECT {
    string sha256 PK "the name IS the hash"
    bytes body "one copy per distinct document"
  }
  URL {
    string url PK
    string digest FK
    string title
    string verdict
  }
  WARC_RECORD {
    int offset "byte position"
    int length "one gzip member"
    string target_uri
    string payload_digest
  }
  CDX_LINE {
    string surt_key PK "com,example)/a"
    string timestamp PK
    int offset FK
    int length
  }
```

Deduplication is not a feature here; it is a consequence of naming objects by
their content. Two identical bodies compute the same digest and therefore
occupy the same path — there is no comparison step to forget.

The CDX key is `(SURT, timestamp)`, and the pair matters: the same URL captured
twice is two rows, which is the entire reason the format exists. Writing the
index with `"w"` instead of merging silently deleted the previous crawl's
captures — the archive kept the records, and nothing could find them.

## 2.6 Character encoding, in precedence order

```mermaid
flowchart TB
  A{"BOM present?"} -->|yes| A1["use it<br/><i>bytes announcing themselves</i>"]:::ok
  A -->|no| B{"charset on the<br/>HTTP header?"}
  B -->|yes| B1{"decodes strictly?"}
  B1 -->|yes| B2["use it"]:::ok
  B1 -->|"no — it lied"| C
  B -->|no| C{"meta charset<br/>in first 2 KB?"}
  C -->|yes| C1{"decodes strictly?"}
  C1 -->|yes| C2["use it"]:::ok
  C1 -->|"no — it lied"| D
  C -->|no| D{"valid UTF-8?"}
  D -->|yes| D1["utf-8"]:::ok
  D -->|no| E["windows-1252<br/><i>decodes every byte,<br/>so it cannot fail</i>"]:::last

  classDef ok fill:#1f5e4b,stroke:#123c30,color:#fff
  classDef last fill:#b07a16,stroke:#7d5610,color:#fff
```

The strictness is the design. `errors="replace"` would honour a false
declaration and destroy the text silently; a strict failure is the only
evidence that a page lied about itself. Twelve stages shipped without this
because the corpus was entirely UTF-8 — and it went unnoticed because link
extraction survives mojibake, so crawls looked perfect while every title and
duplicate-detection fingerprint was corrupt.

## 2.7 Known gaps

Named here rather than drawn as if solved:

| Gap | Consequence |
|---|---|
| No `revisit` WARC records | An unchanged page recaptured writes a full second record. Pinned by a characterisation test. |
| No statistical charset detection | Undeclared Shift-JIS decodes to plausible Latin nonsense. Pinned by a characterisation test. |
| No `429` / `Retry-After` backoff | A rate-limited crawl at volume has no correct behaviour to fall back on. |
| CDX merge rewrites the whole index | Correct, and O(n) per crawl. A real indexer sorts externally. |
| Redirects are not re-validated against the SSRF guard | An allowlisted site redirecting to a private address is followed by the initial fetch. Narrow, because the allowlist is four fixed sites. |
