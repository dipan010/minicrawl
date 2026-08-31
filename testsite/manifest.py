"""Derives manifest.json — the ground truth every crawl is diffed against.

The graph walk here reads spec.PAGES directly: no HTTP, no HTML parsing, no
URL resolution. The crawler must arrive at the same answer the hard way, by
fetching and parsing. That gap is what makes the comparison a real test.

    uv run python -m testsite.manifest > testsite/manifest.json
"""
from __future__ import annotations

import json
from collections import deque

from . import spec

# The policy the crawler is expected to honour from stage 3 onward.
UA = "minicrawl"
MAX_GEN_DEPTH = 0          # /gen/* is a trap: stage 5 caps it, expected set excludes it
SAME_HOST_ONLY = True


def robots_blocks(path: str, port: int) -> bool:
    r = spec.ROBOTS[port]
    if r["disallowed"] == ["*"]:
        return True
    if path in r.get("allowed_exception", []):
        return False
    for rule in r["disallowed"]:
        if rule.endswith("/") and path.startswith(rule):
            return True
        if rule == path:
            return True
    return False


def is_html(path: str) -> bool:
    page = spec.PAGES.get(path)
    return bool(page) and "raw_bytes" not in page


def resolve(path: str, seen_redirects: set[str] | None = None) -> str | None:
    """Follow declared redirects to a terminal page; None on a loop."""
    seen = seen_redirects or set()
    while True:
        page = spec.PAGES.get(path)
        if page is None or "redirect" not in page:
            return path if page else None
        if path in seen:
            return None                     # redirect loop
        seen.add(path)
        path = page["redirect"]


def expected_pages(port: int = spec.PRIMARY) -> list[str]:
    """BFS over the declared graph under the stated policy."""
    out, queue, seen = [], deque(["/"]), {"/"}
    while queue:
        path = queue.popleft()
        if robots_blocks(path, port) or not is_html(path):
            continue
        out.append(path)
        for target in spec.PAGES[path].get("links", []):
            if target.startswith(spec.GEN_PREFIX) and MAX_GEN_DEPTH == 0:
                continue
            final = resolve(target)
            if final is None or final in seen:
                continue
            seen.add(final)
            queue.append(final)
    return sorted(out)


def build() -> dict:
    primary = spec.PRIMARY
    pages = {}
    for path, page in spec.PAGES.items():
        pages[path] = {
            "status": page.get("status", 302 if "redirect" in page else 200),
            "content_type": page.get("content_type", "text/html"),
            "title": page.get("title"),
            "redirect_to": page.get("redirect"),
            "canonical": page.get("canonical"),
            "declared_links": page.get("links", []),
            "flags": page.get("flags", []),
        }
    return {
        "_readme": "Ground truth for the minicrawl test corpus. Regenerate with "
                   "`uv run python -m testsite.manifest > testsite/manifest.json`.",
        "hosts": [spec.host(p) for p in spec.HOST_PORTS],
        "primary_host": spec.host(primary),
        "policy": {"user_agent": UA, "same_host_only": SAME_HOST_ONLY,
                   "max_gen_depth": MAX_GEN_DEPTH},
        "pages": pages,
        "robots": {
            spec.host(p): {"status": r["status"], "disallowed": r["disallowed"],
                           "crawl_delay": r.get("crawl_delay"),
                           "effect": ("allow all (404)" if r["status"] == 404 else
                                      "disallow all (5xx, RFC 9309)" if r["status"] == 500
                                      else "rules apply")}
            for p, r in spec.ROBOTS.items()
        },
        # Each group's hrefs must all normalise to that group's canonical URL.
        # Params are sorted; utm_* and sid are stripped; the query itself is not.
        "normalization_groups": [
            {"name": name,
             "canonical": f"http://{spec.host(primary)}{group['canonical']}",
             "hrefs": group["hrefs"]}
            for name, group in spec.PAGES["/variants"]["variant_groups"].items()
        ],
        # Settled by a 301 from the server, not by the normaliser.
        "redirect_normalized": spec.PAGES["/variants"]["redirect_normalized"],
        # Unparseable: must be dropped at extraction, never resolved or fetched.
        "malformed_hrefs": spec.PAGES["/variants"]["malformed_hrefs"],
        # Extras under these prefixes are trap pages, expected to be bounded
        # rather than absent -- a general trap defence limits a generator, it
        # cannot know to exclude it entirely. Stage 6 removes them on content.
        "trap_prefixes": ["/gen/"],
        "redirect_chains": [{"start": "/r/1", "hops": ["/r/2", "/r/3"], "final": "/a"}],
        "redirect_loops": [["/loop/1", "/loop/2"]],
        "dup_pairs": {
            "exact": [["/dup/exact-1", "/dup/exact-2"]],
            "near": [["/dup/near-1", "/dup/near-2"]],
            "canonical_alias": [["/dup/canonical-source", "/a"]],
        },
        "js_only": {"shell": "/js-only", "reachable_only_after_render": ["/js-only/child"]},
        "conditional_get": ["/etag"],
        "non_html": [p for p, v in spec.PAGES.items() if "raw_bytes" in v],
        "sitemaps": {"index": "/sitemap.xml", "urls": sorted(
            {u for k, v in spec.SITEMAPS.items() if k != "/sitemap.xml" for u in v})},
        # Listed in a sitemap and linked from nowhere. A link-following crawl
        # cannot reach it at any depth.
        "sitemap_only": sorted(p for p, v in spec.PAGES.items()
                               if "sitemap_only" in v.get("flags", [])),
        "always_changes": sorted(p for p, v in spec.PAGES.items()
                                 if "always_changes" in v.get("flags", [])),
        "traps": spec.TRAPS,
        # THE headline assertion: a polite, same-host crawl from "/" finds exactly this.
        "expected_pages": expected_pages(primary),
        # With a headless browser in the loop, one more page becomes reachable:
        # its only inbound link is written by JavaScript.
        "expected_pages_rendered": sorted(
            expected_pages(primary) + spec.PAGES["/js-only"]["js_links"]),
        # With sitemaps read as a second seed source, the orphan appears too.
        "expected_pages_with_sitemaps": sorted(
            set(expected_pages(primary))
            | {p for p, v in spec.PAGES.items() if "sitemap_only" in v.get("flags", [])}),
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
