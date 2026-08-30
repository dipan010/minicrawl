"""Stage 1 — turning a byte string into links.

Three things trip up naive link extraction, and the test corpus has all three:
`<base href>` changes what relative URLs mean, protocol-relative `//host/path`
inherits the scheme, and fragments make two identical URLs look different.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "#")


@dataclass(slots=True)
class Extracted:
    title: str
    links: list[str]
    canonical: str | None
    text: str


def parse(body: bytes, base_url: str) -> Extracted:
    tree = LexborHTMLParser(body)

    # <base href> wins over the document URL for every relative link on the page.
    base = base_url
    if (tag := tree.css_first("base[href]")) and (href := tag.attributes.get("href")):
        base = urljoin(base_url, href)

    links, seen = [], set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(SKIP_SCHEMES):
            continue
        resolved = absolutise(href, base)
        if resolved and resolved not in seen:
            seen.add(resolved)
            links.append(resolved)

    canonical = None
    if (tag := tree.css_first('link[rel="canonical"][href]')):
        canonical = absolutise(tag.attributes.get("href", ""), base)

    title_node = tree.css_first("title")
    body_node = tree.css_first("body")
    return Extracted(
        title=(title_node.text(strip=True) if title_node else ""),
        links=links,
        canonical=canonical,
        text=(body_node.text(separator=" ", strip=True) if body_node else ""),
    )


def absolutise(href: str, base: str) -> str | None:
    """Resolve against base, drop the fragment, and *validate*. None if unusable.

    Validation is not optional here. The corpus contains
    `http://127.0.0.1:8081:/a`, which `urljoin` happily passes through — the
    URL only blows up later, when something reads its port. Touching `.port`
    now converts a crash deep in the fetch loop into a dropped link.
    """
    try:
        resolved = urldefrag(urljoin(base, href)).url
        parts = urlsplit(resolved)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        parts.port                      # raises ValueError on a bad port
        return resolved
    except ValueError:
        return None
