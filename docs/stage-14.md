# Stage 14 — running a crawler versus exposing one

Stage 13 built a front end and ran it on a laptop. This stage puts it on the
internet, and the interesting part is everything that had to change first.

The code is the same. The threat model is not.

## A URL box on a public host is not a UI, it is a service

On your own machine, "type a URL and I will fetch it" is a tool: you chose the
target, the requests leave your own address, and you own whatever follows. Give
the same box a public URL and every one of those facts inverts. A stranger
chooses the target. The requests leave the **host's** address with the
operator's account attached. The complaint arrives at the operator.

Three things follow, and none of them are optional.

### SSRF is the one that actually hurts

"Fetch this URL for me" pointed inward is one of the oldest holes there is.

- `http://169.254.169.254/` is the cloud metadata endpoint. On most providers
  it hands the instance's own credentials to anything that asks from inside.
- `http://127.0.0.1:6379/` is whatever else is running on the box.
- `http://10.0.0.5/` is the rest of the private network.

The guard resolves the hostname and inspects **every address it maps to**,
because checking the string catches nothing: `localtest.me` is a perfectly
ordinary public hostname that resolves to `127.0.0.1`, and a substring test for
`localhost` or a leading `127.` waves it straight through. `ip_address.is_global`
excludes loopback, link-local, RFC 1918, multicast, reserved and the
unspecified address in a single predicate.

The refusal message is deliberately vague and **identical** for a blocked
address and a name that does not resolve. Distinguishing them turns the demo
into a free port scanner: "blocked" versus "no such host" maps the private
network one guess at a time.

### Abuse, which needs no cleverness at all

A stranger can start crawls faster than the crawler finishes them. Without a
cap the host is a free amplifier aimed wherever they like. So: at most three
crawls server-wide, one per visitor per twenty seconds, forty pages, a second
between requests.

Rate limiting is keyed on `X-Forwarded-For`, because behind a platform proxy
every connection appears to come from the proxy. That header is trivially
spoofable, which is exactly why it is used **only** for rate limits and never
for access: a spoofer merely gives themselves a fresh bucket. If access
decisions read it, the same spoof would bypass the guard outright.

### Scope, which is the part that actually settles it

The safest public demo does not take arbitrary targets at all. An allowlist of
sites that exist to be crawled — `quotes.toscrape.com`, `books.toscrape.com`,
this project's own report page, `example.com` — shows precisely the same
machinery and cannot be turned into a weapon. The guards above still run
underneath it, because a list is a policy and defence should not rest on one
line of configuration being right.

## One page, two policies

The page asks the server what is allowed rather than shipping two variants of
itself. `GET /config` returns the policy; the page swaps its free-text field
for a picker when a list is in force, and tightens its own number inputs to
the caps. The server clamps regardless — a limit the client can raise is not a
limit — but a form that offers what will be refused is a bad form.

`MINICRAWL_PUBLIC=1` opts in, and **only** an exact `1`. A deployment that
forgets the flag gets the restrictive policy, never the permissive one. The
direction matters: defaults should fail toward safety, and `"true"` not working
is the correct amount of pedantry for a flag that governs this.

## What the CLI keeps

None of this narrows the command line. `--ignore-robots` is still there,
loopback is still crawlable, and there is no cooldown, because a human running
a CLI owns the consequences of what they point it at. The asymmetry is the
whole idea: **the limits belong to exposure, not to the crawler.**

## The bug the container found, and no local test could have

The image installs the two core dependencies and nothing else. It would not
start:

```
File "/app/minicrawl/frontier/redis.py", line 46, in <module>
    import redis as redis_lib
ModuleNotFoundError: No module named 'redis'
```

`frontier/__init__` imports the Redis frontier, which imported `redis` at
module scope — so importing `minicrawl` at all required an **optional** extra,
and the README's claim of two core dependencies was false. Every development
machine here has the `distributed` extra installed, so the import always
succeeded and the bug was invisible for five stages.

The import is now guarded, and the failure moved to the moment someone asks
for a Redis frontier, where it can say what to install instead of dying on a
bare `ModuleNotFoundError`.

Pinning it needed a test that actually makes the module unavailable — reloading
with `redis` installed proves nothing — so the test patches `__import__` to
refuse it. Verified the honest way: with the guard removed, the test fails.

This is the fifth appearance of the theme, with a new twist. The earlier ones
were bugs the corpus could not expose. This was a bug the **environment** could
not expose: a dependency that is always present cannot be observed to be
missing.

## What was deliberately not built

- **Arbitrary public targets.** The guards exist and work; the allowlist is
  the belt to their braces. Opening it up is one constant, and the reasons not
  to are in this file rather than in a comment nobody reads.
- **Authentication.** A public demo with a login is not a demo. The answer to
  "who may crawl anything" is "someone running it locally".
- **A queue.** Over the concurrency cap, the server says it is busy rather than
  promising work it may not get to.
- **Redirect re-validation.** The seed is checked, and `same_host` keeps the
  crawl on the seed's host, but a redirect from an allowlisted site to a
  private address is followed by the initial fetch. That is a real gap, narrow
  because the allowlist is four fixed sites, and it is named here rather than
  left for someone to find.
