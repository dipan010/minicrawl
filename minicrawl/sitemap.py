"""Stage 8 — the seeds nobody links to.

A link-following crawl can only reach what is linked. `/orphan` in the test
corpus is listed in a sitemap and linked from nowhere, so no depth limit, no
politeness setting and no amount of parsing cleverness will find it. Sitemaps
are a second, independent source of seeds, and they are the only way in.

Two document types share one namespace, and the difference matters:

    <sitemapindex>  a list of OTHER sitemaps       -> fetch each one
    <urlset>        a list of pages, with lastmod  -> these are the seeds

An index can nest, so following it is a small crawl of its own — and one with
the same trap potential as any other, which is why the recursion is bounded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MAX_INDEX_DEPTH = 3
MAX_URLS = 50_000               # the protocol's own per-file limit


@dataclass(slots=True)
class SitemapEntry:
    url: str
    lastmod: str | None = None


@dataclass(slots=True)
class Sitemap:
    is_index: bool
    entries: list[SitemapEntry]


def parse(body: bytes) -> Sitemap:
    """Parse a sitemap or a sitemap index. Malformed XML yields nothing.

    A broken sitemap must not end a crawl: it is a hint, not a contract, and
    plenty of real ones are truncated or served as HTML error pages.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return Sitemap(is_index=False, entries=[])

    tag = _localname(root.tag)
    is_index = tag == "sitemapindex"
    child = "sitemap" if is_index else "url"

    entries: list[SitemapEntry] = []
    for node in root.iter():
        if _localname(node.tag) != child:
            continue
        loc = node.find(f"{NS}loc")
        if loc is None:
            loc = next((c for c in node if _localname(c.tag) == "loc"), None)
        if loc is None or not (loc.text or "").strip():
            continue
        lastmod = node.find(f"{NS}lastmod")
        entries.append(SitemapEntry(
            url=loc.text.strip(),
            lastmod=(lastmod.text or "").strip() if lastmod is not None else None))
        if len(entries) >= MAX_URLS:
            break
    return Sitemap(is_index=is_index, entries=entries)


def _localname(tag: str) -> str:
    return re.sub(r"^\{.*\}", "", tag)
