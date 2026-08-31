"""Declarative spec for the synthetic test site.

This module is the *ground truth*. The HTTP server (server.py) renders it, and
manifest.py serialises it. Every crawler stage is verified by diffing a real
crawl against what is declared here.

Each page declares:
  links  -- resolved target paths (the truth a correct crawler must recover)
  raw    -- optional {resolved_path: literal_href}, how the link is written in
            the HTML. This is where relative / protocol-relative / base-href /
            query-variant trickiness lives.
  flags  -- tags the tests assert on (js_only, near_dup, trap, ...)
"""

HOST_PORTS = [8081, 8082, 8083, 8084]
PRIMARY = HOST_PORTS[0]


def host(port: int) -> str:
    return f"127.0.0.1:{port}"


# --- robots.txt, one variant per host -------------------------------------
# RFC 9309: a 404 means "allow all"; a 5xx means "disallow everything".
ROBOTS = {
    8081: {
        "status": 200,
        "body": (
            "User-agent: *\n"
            "Disallow: /private/\n"
            "Disallow: /*.pdf$\n"
            "Crawl-delay: 0.2\n"
            "\n"
            "User-agent: minicrawl\n"
            "Disallow: /private/\n"
            "Disallow: /files/\n"
            "Allow: /private/public-corner\n"
            "Crawl-delay: 0.2\n"
            "\n"
            "Sitemap: http://127.0.0.1:8081/sitemap.xml\n"
        ),
        "disallowed": ["/private/secret", "/files/report.pdf", "/files/logo.png"],
        "allowed_exception": ["/private/public-corner"],
        "crawl_delay": 0.2,
    },
    8082: {"status": 404, "body": "not found", "disallowed": [], "crawl_delay": None},
    8083: {"status": 500, "body": "boom", "disallowed": ["*"], "crawl_delay": None},
    8084: {
        "status": 200,
        "body": "User-agent: *\nDisallow: /gen/\n",
        "disallowed": ["/gen/"],
        "crawl_delay": None,
    },
}

TEXT_A = (
    "Politeness is the part of crawling that separates a tool from a nuisance. "
    "A crawler that ignores robots.txt and hammers a host at full concurrency is "
    "indistinguishable from a denial of service, regardless of intent."
)
TEXT_NEAR = (
    "Politeness is the part of crawling that separates a tool from a nuisance. "
    "A crawler that ignores robots.txt and floods a host at full concurrency is "
    "hard to distinguish from a denial of service, whatever the intent."
)

PAGES: dict[str, dict] = {
    "/": {
        "title": "Index",
        "body": "<p>Entry point for the minicrawl test corpus.</p>",
        "links": [
            "/a", "/b", "/docs/", "/variants", "/dup/exact-1", "/dup/near-1",
            "/dup/canonical-source", "/r/1", "/loop/1", "/slow", "/js-only",
            "/etag", "/private/secret", "/private/public-corner", "/gen/1",
            "/files/report.pdf", "/hosts",
        ],
    },
    "/a": {
        "title": "Page A",
        "body": f"<p>{TEXT_A}</p>",
        "links": ["/b", "/c"],
        # /b written protocol-relative, /c written as a bare relative path
        "raw": {"/b": "//127.0.0.1:8081/b", "/c": "c"},
    },
    "/b": {"title": "Page B", "body": "<p>Frontier order decides crawl shape.</p>",
           "links": ["/c", "/"], "raw": {"/": "./"}},
    "/c": {"title": "Page C", "body": "<p>Cycles are the default, not the exception.</p>",
           "links": ["/a"]},

    # --- base href + relative resolution ---------------------------------
    "/docs/": {
        "title": "Docs index",
        "base": "http://127.0.0.1:{port}/docs/",
        "body": "<p>Relative links resolved against &lt;base href&gt;.</p>",
        "links": ["/docs/one", "/docs/two", "/docs/sub/three"],
        "raw": {"/docs/one": "one", "/docs/two": "two", "/docs/sub/three": "sub/three"},
    },
    "/docs/one": {"title": "Docs one", "body": "<p>One.</p>", "links": ["/docs/two"],
                  "raw": {"/docs/two": "../docs/two"}},
    "/docs/two": {"title": "Docs two", "body": "<p>Two.</p>", "links": ["/docs/sub/three"]},
    "/docs/sub/three": {"title": "Docs three", "body": "<p>Three.</p>", "links": ["/docs/"],
                        "raw": {"/docs/": "../"}},

    # --- URL variants that all normalise to /a ---------------------------
    "/variants": {
        "title": "URL variants",
        "body": "<p>Every link below is the same resource.</p>",
        "links": ["/a"],
        # Three distinct contracts, deliberately not one list:
        #   bare      -> all collapse to /a
        #   queried   -> all collapse to /a?a=1&b=2 (params sorted, tracking and
        #                session ids stripped) -- NOT to bare /a. A normaliser
        #                that drops every query string is broken, not thorough.
        #   malformed -> dropped entirely, never resolved
        "variant_groups": {
            "bare": {
                "canonical": "/a",
                "hrefs": ["/a", "/a?", "/a#section", "/a#other",
                          "http://127.0.0.1:8081/a", "http://127.0.0.1:8081/./a",
                          "http://127.0.0.1:8081/x/../a"],
            },
            "queried": {
                "canonical": "/a?a=1&b=2",
                "hrefs": ["/a?b=2&a=1", "/a?a=1&b=2", "/a?a=1&b=2&",
                          "/a?a=1&b=2&utm_source=news", "/a?sid=99a1&a=1&b=2"],
            },
        },
        # NOT a normalisation case: /a and /a/ are formally distinct resources.
        # The server settles it with a 301, and the crawler learns by following.
        "redirect_normalized": {"/a/": "/a"},
        "malformed_hrefs": ["http://127.0.0.1:8081:/a"],
        "flags": ["normalization"],
    },

    # --- duplicate content ------------------------------------------------
    "/dup/exact-1": {"title": "Duplicate", "body": f"<p>{TEXT_A}</p>", "links": ["/"],
                     "flags": ["exact_dup"]},
    "/dup/exact-2": {"title": "Duplicate", "body": f"<p>{TEXT_A}</p>", "links": ["/"],
                     "flags": ["exact_dup"]},
    "/dup/near-1": {"title": "Near duplicate one", "body": f"<p>{TEXT_A}</p>",
                    "links": ["/dup/near-2", "/dup/exact-1", "/dup/exact-2"],
                    "flags": ["near_dup"]},
    "/dup/near-2": {"title": "Near duplicate two", "body": f"<p>{TEXT_NEAR}</p>",
                    "links": ["/"], "flags": ["near_dup"]},
    "/dup/canonical-source": {
        "title": "Canonical points elsewhere",
        "body": "<p>This page declares /a as its canonical URL.</p>",
        "canonical": "/a", "links": ["/"], "flags": ["canonical"],
    },

    # --- redirects --------------------------------------------------------
    "/r/1": {"redirect": "/r/2", "status": 302, "flags": ["redirect_chain"]},
    "/r/2": {"redirect": "/r/3", "status": 301, "flags": ["redirect_chain"]},
    "/r/3": {"redirect": "/a", "status": 302, "flags": ["redirect_chain"]},
    "/loop/1": {"redirect": "/loop/2", "status": 302, "flags": ["trap", "redirect_loop"]},
    "/loop/2": {"redirect": "/loop/1", "status": 302, "flags": ["trap", "redirect_loop"]},

    # --- timing -----------------------------------------------------------
    "/slow": {"title": "Slow page", "body": "<p>Served after a delay.</p>",
              "links": ["/"], "delay": 0.8, "flags": ["slow"]},
    "/hang": {"title": "Hang", "body": "<p>never</p>", "links": [], "delay": 30.0,
              "flags": ["trap", "hang"]},

    # --- JS-only ----------------------------------------------------------
    "/js-only": {
        "title": "Client rendered",
        "raw_html": (
            '<div id="app"></div>\n'
            '<script>document.getElementById("app").innerHTML='
            '"<h2>Rendered client side</h2><p>Only a real browser sees this text, '
            'and only a browser finds the link below.</p>'
            '<a href=\\"/js-only/child\\">child</a>";</script>'
        ),
        "links": [],
        "js_links": ["/js-only/child"],
        "flags": ["js_only"],
    },
    "/js-only/child": {"title": "JS child", "body": "<p>Reachable only after rendering.</p>",
                       "links": ["/"], "flags": ["js_only_child"]},

    # --- conditional GET --------------------------------------------------
    "/etag": {"title": "Cacheable", "body": "<p>Has ETag and Last-Modified.</p>",
              "links": ["/"], "etag": True, "flags": ["conditional_get"]},

    # --- robots-excluded --------------------------------------------------
    "/private/secret": {"title": "Secret", "body": "<p>Should never be fetched.</p>",
                        "links": ["/"], "flags": ["robots_disallowed"]},
    "/private/public-corner": {"title": "Allowed exception",
                               "body": "<p>Allow: beats Disallow: by specificity.</p>",
                               "links": ["/"], "flags": ["robots_allow_exception"]},

    # --- non-HTML ---------------------------------------------------------
    "/files/report.pdf": {"content_type": "application/pdf", "raw_bytes": b"%PDF-1.4 fake",
                          "links": [], "flags": ["non_html"]},
    "/files/logo.png": {"content_type": "image/png", "raw_bytes": b"\x89PNG\r\n\x1a\n fake",
                        "links": [], "flags": ["non_html"]},

    # --- cross-host -------------------------------------------------------
    "/hosts": {"title": "Other hosts", "body": "<p>The same corpus on four origins.</p>",
               "links": ["/"], "cross_host": True, "flags": ["cross_host"]},
}

# /gen/<n> is generated on the fly: an unbounded chain with a fat body.
GEN_PREFIX = "/gen/"
GEN_BODY_BYTES = 64 * 1024   # fat enough that an uncapped crawl notices

SITEMAPS = {
    "/sitemap.xml": ["/sitemap-1.xml", "/sitemap-2.xml"],   # a sitemap *index*
    "/sitemap-1.xml": ["/", "/a", "/b", "/c"],
    "/sitemap-2.xml": ["/docs/", "/docs/one", "/docs/two", "/docs/sub/three", "/etag"],
}

# Traps a naive crawler must survive, and the stage that fixes each.
TRAPS = {
    "/loop/1": "redirect loop — fixed in stage 5 (normalise + seen-set on redirects)",
    "/gen/1": "unbounded generator — fixed in stage 5 (byte cap + path-depth cap)",
    "/hang": "no response — fixed in stage 4 (per-request timeout)",
}
