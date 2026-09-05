"""Stage 13 — deciding what encoding a page is in.

Every page in the corpus was UTF-8 for twelve stages, so nothing ever caught
that `parse()` assumed it. Pointing the crawler at the real web found it in
minutes: a page in windows-1252 came back as `Caf� na�ve`, and the
crawler carried on cheerfully, because link extraction survives mojibake —
hrefs are ASCII. What breaks is everything downstream that reads TEXT: titles,
word counts, and the shingles simhash computes over. A near-duplicate detector
comparing two mangled strings still returns an answer. Just not a right one.

THE ORDER OF PRECEDENCE, AND WHY

1. The BOM, if present. It is the encoding announcing itself in the bytes,
   and it cannot be a stale copy-paste the way a meta tag can.
2. The HTTP `Content-Type` charset. The server knows what it just encoded;
   the document only repeats what its author typed. WHATWG gives the
   transport layer the last word for exactly this reason.
3. `<meta charset>` or `<meta http-equiv="content-type">` in the document.
4. UTF-8, tried strictly — so that failure is detectable rather than papered
   over with replacement characters.
5. windows-1252. Not a guess: it is the fallback the HTML standard mandates,
   because the pre-UTF-8 web was full of it and because it decodes every
   possible byte, so this step cannot fail.

The step everyone omits is checking whether the declaration was TRUE. A page
that says UTF-8 and is not is common enough that the standard has a recovery
path, and the corpus now serves one deliberately.
"""
from __future__ import annotations

import codecs
import re

# Only the head is scanned: a charset declaration is required to appear early,
# and reading megabytes of body to look for one is how a parser becomes the
# slow part of a crawl.
META_SCAN_BYTES = 2048

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""",
                           re.IGNORECASE)
_HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""",
                             re.IGNORECASE)

BOMS = ((codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))

FALLBACK = "windows-1252"


def charset_from_header(content_type: str | None) -> str | None:
    if not content_type:
        return None
    found = _HEADER_CHARSET.search(content_type)
    return found.group(1).lower() if found else None


def charset_from_meta(body: bytes) -> str | None:
    found = _META_CHARSET.search(body[:META_SCAN_BYTES])
    return found.group(1).decode("ascii", "replace").lower() if found else None


def _usable(name: str | None) -> str | None:
    """A codec Python actually has. Real pages name encodings that do not
    exist ('utf8mb4', 'unicode', typos); an unknown name is one more hint that
    did not work out, not a crash."""
    if not name:
        return None
    try:
        codecs.lookup(name)
        return name
    except LookupError:
        return None


def decode(body: bytes, content_type: str | None = None) -> tuple[str, str, str]:
    """Return (text, encoding used, how it was decided).

    The third value exists so the decision is auditable. A crawler that
    silently picks an encoding gives you no way to tell a correct guess from a
    lucky one, and this project's rule is that a number you cannot explain
    will be believed by whoever reads it next.
    """
    for bom, encoding in BOMS:
        if body.startswith(bom):
            return body.decode(encoding, "replace"), encoding, "bom"

    for name, source in ((charset_from_header(content_type), "http header"),
                         (charset_from_meta(body), "meta tag")):
        usable = _usable(name)
        if not usable:
            continue
        try:
            # Strict on purpose. Errors are the only evidence that the
            # declaration was wrong, and "replace" would destroy it.
            return body.decode(usable), usable, source
        except (UnicodeDecodeError, LookupError):
            continue                      # it lied; keep looking

    try:
        return body.decode("utf-8"), "utf-8", "utf-8 decoded cleanly"
    except UnicodeDecodeError:
        pass

    # Cannot fail: every byte sequence is valid windows-1252.
    return body.decode(FALLBACK, "replace"), FALLBACK, "fallback"
