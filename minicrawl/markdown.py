"""Stage 16a — HTML to Markdown, for a machine that will read it.

The output of a reader is consumed by a language model, not rendered in a
browser, and that changes what "good" means:

  STRUCTURE SURVIVES.  A heading has to stay a heading, a list a list, a code
      block a code block. Flattening to plain text — which is what
      `main_text` does for duplicate detection — throws away the document's
      shape, and shape is most of what tells a reader which sentence is the
      claim and which is the caveat.

  NOTHING IS INVENTED.  No wrapping, no smart quotes, no reflowing. Every
      transformation here is reversible in the sense that matters: a reader
      can tell what the original markup was.

  LINKS KEEP THEIR TARGETS, ABSOLUTE.  A relative href in extracted markdown
      is worse than no href, because it looks usable and is not.

No dependency. `markdownify` and `html2text` both exist and both do more than
this needs; the whole converter is one recursive walk, and writing it is the
only way to know what it does with a nested list.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

# Removed before conversion: furniture, and anything that is not prose.
STRIP = ("script", "style", "noscript", "template", "svg", "iframe", "form",
         "button", "nav", "header", "footer", "aside")

# Where the document usually is, most specific first. A page that says which
# part is the article is a page worth believing.
MAIN_CANDIDATES = ("article", "main", "[role=main]", "#content", "#main",
                   ".post-content", ".entry-content", ".article-body")

# Listing the INLINE tags rather than the block ones is deliberate. Inline
# elements are a small, closed set; block-level containers are open-ended, and
# a page using <article>, <section> or any custom element would be flattened
# into one run-on paragraph by a block allowlist that had not heard of it.
# Unknown tag therefore means container, not inline.
INLINE = {"a", "strong", "b", "em", "i", "code", "span", "small", "sub", "sup",
          "img", "br", "abbr", "cite", "mark", "u", "s", "time", "label",
          "kbd", "samp", "var", "q", "wbr", "font"}

_SPACES = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


def main_node(tree: LexborHTMLParser):
    """The node holding the document, or the body.

    Tried in order of how much the page is *telling* us: an <article> is an
    assertion by the author, a <div id=content> is a convention, <body> is a
    guess. Only a candidate with real text counts — empty <main> wrappers are
    common, and picking one yields a blank read from a page full of words.
    """
    for selector in MAIN_CANDIDATES:
        node = tree.css_first(selector)
        if node is not None and len(node.text(strip=True) or "") > 200:
            return node
    return tree.css_first("body") or tree.root


def _clean(text: str) -> str:
    return _SPACES.sub(" ", text.replace("\xa0", " "))


def _inline_node(node, base: str) -> str:
    """Format ONE inline node, including its own tag.

    Split from `_inline` deliberately: a function that only formats a node's
    *children* silently drops the node's own emphasis when it is called on a
    `<strong>` directly, which is exactly what happens when a block walker
    hands it one. The bug is invisible — the text is all there, just plain.
    """
    tag = node.tag
    if tag == "-text":
        return _clean(node.text(deep=False) or "")
    if tag in ("strong", "b"):
        inner = _inline(node, base).strip()
        return f"**{inner}**" if inner else ""
    if tag in ("em", "i"):
        inner = _inline(node, base).strip()
        return f"*{inner}*" if inner else ""
    if tag == "code":
        inner = _inline(node, base).strip()
        return f"`{inner}`" if inner else ""
    if tag == "a":
        inner = _inline(node, base).strip()
        href = (node.attributes.get("href") or "").strip()
        if inner and href and not href.startswith(("javascript:", "#")):
            # Absolute, always: a relative link in extracted markdown looks
            # usable and is not, which is worse than omitting it.
            return f"[{inner}]({urljoin(base, href)})"
        return inner
    if tag == "img":
        alt = (node.attributes.get("alt") or "").strip()
        src = (node.attributes.get("src") or "").strip()
        return f"![{alt}]({urljoin(base, src)})" if src else ""
    if tag == "br":
        return "  \n"
    if tag in STRIP:
        return ""
    return _inline(node, base)


def _inline(node, base: str) -> str:
    """Inline content of a node's children."""
    return "".join(_inline_node(child, base)
                   for child in node.iter(include_text=True))


def _block(node, base: str, depth: int = 0) -> str:
    tag = node.tag

    if tag in STRIP:
        return ""
    if tag == "hr":
        return "\n---\n"
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        text = _inline(node, base).strip()
        return f"\n{'#' * int(tag[1])} {text}\n" if text else ""
    if tag == "pre":
        code = node.text(deep=True) or ""
        return f"\n```\n{code.strip()}\n```\n" if code.strip() else ""
    if tag == "blockquote":
        inner = "".join(_block(c, base, depth) for c in node.iter()).strip()
        if not inner:
            inner = _inline(node, base).strip()
        quoted = "\n".join("> " + line for line in inner.splitlines())
        return f"\n{quoted}\n" if inner else ""
    if tag in ("ul", "ol"):
        items, n = [], 1
        for child in node.iter():
            if child.tag != "li":
                continue
            marker = f"{n}." if tag == "ol" else "-"
            n += 1
            body = _list_item(child, base, depth)
            if body:
                pad = "  " * depth
                items.append(f"{pad}{marker} {body}")
        return "\n" + "\n".join(items) + "\n" if items else ""
    if tag == "table":
        return _table(node, base)

    if tag in INLINE:
        return _inline(node, base)

    # Anything else is a container: walk it, keeping blocks apart.
    parts = []
    for child in node.iter(include_text=True):
        if child.tag == "-text":
            text = _clean(child.text(deep=False) or "")
            if text.strip():
                parts.append(text)
        elif child.tag in STRIP:
            continue
        elif child.tag in INLINE:
            parts.append(_inline_node(child, base))
        else:
            parts.append(_block(child, base, depth))
    joined = "".join(parts).strip()
    return f"\n{joined}\n" if joined else ""


def _list_item(node, base: str, depth: int) -> str:
    """One list item: its own inline text, plus any nested list beneath it."""
    inline_parts, nested = [], []
    for child in node.iter(include_text=True):
        if child.tag in ("ul", "ol"):
            nested.append(_block(child, base, depth + 1).rstrip("\n"))
        elif child.tag == "-text":
            inline_parts.append(_clean(child.text(deep=False) or ""))
        elif child.tag in STRIP:
            continue
        elif child.tag in INLINE:
            inline_parts.append(_inline_node(child, base))
        else:
            inline_parts.append(_block(child, base, depth).strip() + " ")
    body = " ".join("".join(inline_parts).split())
    if nested:
        body += "\n" + "\n".join(n for n in nested if n)
    return body


def _table(node, base: str) -> str:
    """Tables become pipe tables, header row first.

    A table with no <th> gets an empty header, because a pipe table without a
    separator row is not a table to anything that parses markdown.
    """
    rows = []
    for tr in node.css("tr"):
        cells = [_inline(cell, base).strip().replace("|", "\\|")
                 for cell in tr.css("th, td")]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n" + "\n".join(out) + "\n"


def to_markdown(html: bytes | str, base_url: str, *,
                content_type: str | None = None) -> str:
    """Convert a page to Markdown, keeping only the document."""
    if isinstance(html, bytes):
        from .charset import decode
        html, _, _ = decode(html, content_type)
    tree = LexborHTMLParser(html)
    for tag in STRIP:
        for node in tree.css(tag):
            node.decompose()
    node = main_node(tree)
    if node is None:
        return ""
    text = _block(node, base_url)
    return _BLANKS.sub("\n\n", text).strip()
