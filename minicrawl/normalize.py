"""Stage 5 — one resource, one URL.

Stage 2 fetched `/a` ten times because the seen-set held raw strings and the
corpus spells that page ten ways. Worse, the unnormalised `/a/` made the
relative link `c` resolve to `/a/c` — a URL that does not exist, invented by
the crawler out of its own sloppiness. Normalisation is a correctness feature
before it is an efficiency one.

The rules below are the safe ones: each is a transformation that RFC 3986 says
cannot change which resource is addressed, plus two policy choices that are
marked as such. Anything more aggressive (dropping `index.html`, collapsing
`//`, lowercasing the path) can change meaning on real servers and is left out.
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

# Parameters that identify the *visitor*, not the resource. Two URLs differing
# only in these address the same page, so they must collapse to one.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref", "referrer", "sid", "sessionid", "session_id", "phpsessid", "jsessionid",
})

# Unreserved characters (RFC 3986 §2.3) must never stay percent-encoded.
_SAFE_PATH = "/~-._!$&'()*+,;=:@"


def normalize(url: str) -> str | None:
    """Canonical form of `url`, or None if it is not a usable http(s) URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None

    try:
        port = parts.port
    except ValueError:
        return None

    host = parts.hostname.lower()
    if host.endswith(".") and len(host) > 1:
        host = host[:-1]                       # the root label is not part of identity
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"

    return urlunsplit((scheme, netloc, normalize_path(parts.path),
                       normalize_query(parts.query), ""))   # fragment always dropped


def normalize_path(path: str) -> str:
    """Resolve dot segments and normalise percent-encoding. Keep the trailing slash.

    Stripping the trailing slash is the tempting rule, and it is wrong. `/a` and
    `/a/` are formally distinct resources and servers really do serve different
    things at each — this project's own corpus serves `/docs/` and 404s on
    `/docs`, which is exactly what a directory-style route does everywhere.
    A normaliser that "helpfully" strips the slash invents 404s.

    The right mechanism is the server's: it answers `/a/` with a 301 to `/a`,
    and the crawler learns the two are one by *following the redirect* and
    recording where it landed. One request buys the answer, and the answer is
    authoritative instead of guessed.
    """
    path = quote(unquote(path or "/"), safe=_SAFE_PATH)
    if not path.startswith("/"):
        path = "/" + path
    trailing = path.endswith("/") and len(path) > 1
    # posixpath.normpath collapses "." and ".." and duplicate slashes, and eats
    # a trailing slash we may need to put back.
    resolved = posixpath.normpath(path)
    if trailing and not resolved.endswith("/"):
        resolved += "/"
    return resolved


def normalize_query(query: str) -> str:
    """Drop tracking parameters, sort the rest. An empty result drops the `?`."""
    if not query:
        return ""
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    return urlencode(sorted(pairs))


# --- URL shape, for trap detection ---------------------------------------

_NUMERIC = re.compile(r"^\d+$")


def url_shape(url: str) -> str:
    """The URL with its numeric path segments replaced by a placeholder.

    `/gen/1`, `/gen/2` and `/gen/9999` all have shape `/gen/<num>`. Counting
    shapes rather than URLs is how a crawler notices that a site is generating
    pages at it, which no amount of URL normalisation can detect — every one of
    those URLs is genuinely distinct.
    """
    parts = urlsplit(url)
    segments = ["<num>" if _NUMERIC.match(s) else s for s in parts.path.split("/")]
    return f"{parts.netloc}{'/'.join(segments)}"
