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

# Used ONLY by the exact-duplicate pair. /a has its own text: when /a shared
# this paragraph, /a was a genuine near-duplicate of /dup/exact-1 (distance 4),
# and no threshold should be asked to pretend otherwise.
TEXT_A = (
    "Politeness is the part of crawling that separates a tool from a nuisance. "
    "A crawler that ignores robots.txt and hammers a host at full concurrency is "
    "indistinguishable from a denial of service, regardless of intent."
)
TEXT_PAGE_A = (
    "Redirects mean the URL you asked for and the URL you received are two "
    "different things. Every relative link on the response resolves against "
    "where you landed, not against what you requested, and a crawler that "
    "conflates them will quietly invent URLs that never existed."
)
# The near-duplicate pair needs its OWN base text. If it shared TEXT_A with the
# exact pair, then /dup/near-1 would be roughly as similar to /dup/exact-1 as it
# is to /dup/near-2, and no threshold could tell the declared pairs apart -- the
# corpus would be incapable of testing near-duplicate detection at all.
TEXT_NEAR_LONG = (
    "The frontier is the queue of URLs a crawler has discovered but not yet "
    "visited, and its ordering discipline is the entire crawl strategy. A "
    "first-in-first-out queue produces a breadth-first sweep that covers a site "
    "evenly and shallowly. A stack produces a depth-first descent that tunnels "
    "into one corner and is almost never what anybody wanted. Replace the queue "
    "with a heap and the same loop becomes a priority crawler that spends its "
    "budget where the value is highest. None of this changes the fetching code, "
    "the parsing code, or the storage layer. It changes one data structure, and "
    "the character of the whole crawl changes with it. "
    "That is worth noticing, because it means the hardest decisions in a crawler "
    "are rarely about protocols or parsing. They are about what to look at next, "
    "and that decision lives in a single collection whose interface fits on one "
    "screen. A crawler that keeps its frontier behind an interface can change "
    "strategy without changing anything else. A crawler that does not will have "
    "its strategy welded into the main loop forever, and every later question "
    "about priority, politeness or persistence becomes a rewrite instead of a "
    "substitution. "
    "The same argument applies to the seen set. What you put into it defines "
    "what the crawler believes a page is. Raw URL strings mean that two "
    "spellings of one address count as two pages, and the crawler cheerfully "
    "pays twice for the same bytes. Normalised URLs mean it pays once. Content "
    "hashes mean it notices that two different addresses served the same "
    "document. Similarity fingerprints mean it notices that they served almost "
    "the same document, which is the case that actually dominates a real crawl. "
    "Each of those is a different answer to the same question, and each costs "
    "more than the last. "
    "Politeness is the constraint that makes all of this harder than it looks. "
    "A crawler with no rate limit is a denial of service with good intentions. "
    "A crawler with a global rate limit is polite to nobody in particular and "
    "slow for everyone. The rate limit has to be per origin, which means the "
    "queue has to know about origins, which means the innocuous-looking "
    "collection at the centre of the design is suddenly load-bearing for "
    "correctness, throughput and ethics at the same time. "
    "None of these decisions are visible from the outside. A crawl either "
    "returns the pages or it does not, and the difference between a good "
    "implementation and a bad one shows up as a support ticket from somebody "
    "whose server fell over, or as a bill for storage of documents that were "
    "all the same document."
)

# The near-duplicate is the SAME page with two words edited. That is what a
# near-duplicate looks like in the wild -- a corrected sentence, a swapped
# synonym -- and it is the case the 64-bit/3-bit simhash threshold assumes.
TEXT_NEAR_1 = TEXT_NEAR_LONG
TEXT_NEAR_2 = TEXT_NEAR_LONG.replace("not yet visited", "not yet fetched") \
                            .replace("what anybody wanted", "what anyone wanted")

PAGES: dict[str, dict] = {
    "/": {
        "title": "Index",
        "body": "<p>Entry point for the minicrawl test corpus.</p>",
        "links": [
            "/a", "/b", "/docs/", "/variants", "/dup/exact-1", "/dup/exact-2",
            "/dup/near-1",
            "/dup/canonical-source", "/r/1", "/loop/1", "/slow", "/js-only",
            "/etag", "/volatile", "/compressed",
            "/encoded/latin1", "/encoded/mislabelled", "/encoded/undeclared",
            "/private/secret",
            "/private/public-corner", "/gen/1",
            "/files/report.pdf", "/hosts",
        ],
    },
    "/a": {
        "title": "Page A",
        "body": f"<p>{TEXT_PAGE_A}</p>",
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
    "/dup/near-1": {"title": "Near duplicate one", "body": f"<p>{TEXT_NEAR_1}</p>",
                    "links": ["/dup/near-2", "/dup/exact-1", "/dup/exact-2"],
                    "flags": ["near_dup"]},
    "/dup/near-2": {"title": "Near duplicate two", "body": f"<p>{TEXT_NEAR_2}</p>",
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
    # Every HTML page carries an ETag (see server.py); this one is the declared
    # example the freshness tests are written against.
    "/etag": {"title": "Cacheable", "body": "<p>Has ETag and Last-Modified.</p>",
              "links": ["/"], "flags": ["conditional_get"]},

    # Changes on every single request, so its validator never matches. The
    # counterpart to /etag: one page that is always fresh and one that is never
    # fresh is what an adaptive recrawl interval has to tell apart.
    "/volatile": {"title": "Volatile", "body": "<p>Different every time.</p>",
                  "links": ["/"], "volatile": True, "flags": ["always_changes"]},

    # --- content-encoding -------------------------------------------------
    # Served gzipped. httpx decodes transparently, so the body a crawler holds
    # no longer matches the Content-Encoding and Content-Length headers that
    # came with it. Nothing before stage 11 noticed or cared; an archive writer
    # that emits that pair unchanged produces a record no reader will accept.
    "/compressed": {"title": "Compressed",
                    "body": "<p>This page is served with Content-Encoding: gzip, "
                            "so the bytes on the wire are not the bytes you parse.</p>",
                    "links": ["/"], "gzip": True, "flags": ["content_encoding"]},

    # --- character encodings ----------------------------------------------
    # Everything else in this corpus is UTF-8, which is exactly why the crawler
    # shipped twelve stages mangling anything that is not. These two are the
    # cases the real web actually serves.
    #
    # The header tells the truth here: Content-Type says windows-1252 and the
    # bytes are windows-1252. A decoder that assumes UTF-8 produces mojibake
    # from a page that told it everything it needed.
    "/encoded/latin1": {"title": "Latin-1 caf\u00e9",
                        "body": "<p>Na\u00efve r\u00e9sum\u00e9s, \u00a3 and \u00bd, "
                                "in windows-1252 with an honest header.</p>",
                        "links": ["/"], "charset": "windows-1252",
                        "flags": ["non_utf8"]},

    # Header and document DISAGREE: the HTTP header says windows-1252 (true),
    # the <meta> says UTF-8 (false). WHATWG gives the transport layer the last
    # word, because the server knows what it just encoded and the document is
    # only repeating what its author typed.
    "/encoded/mislabelled": {"title": "Mislabelled caf\u00e9",
                             "body": "<p>Header says windows-1252, meta says UTF-8. "
                                     "The header wins: na\u00efve r\u00e9sum\u00e9.</p>",
                             "links": ["/"], "charset": "windows-1252",
                             "declared_charset": "utf-8",
                             "flags": ["non_utf8", "lying_charset"]},

    # NOTHING trustworthy: no charset on the header at all, and the only
    # declaration in the document is wrong. Every hint the standard offers has
    # been exhausted, so the decoder has to notice that UTF-8 does not decode
    # and recover. This is the page that makes the fallback a rule instead of
    # a comment.
    "/encoded/undeclared": {"title": "Undeclared caf\u00e9",
                            "body": "<p>No charset on the header, a wrong one in "
                                    "the document: na\u00efve r\u00e9sum\u00e9.</p>",
                            "links": ["/"], "charset": "windows-1252",
                            "declared_charset": "utf-8", "omit_charset_header": True,
                            "flags": ["non_utf8", "lying_charset", "undeclared"]},

    # --- reachable only from the sitemap ----------------------------------
    # NOTHING links here. A link-following crawl cannot find it at any depth,
    # which is the entire argument for reading sitemaps.
    "/orphan": {"title": "Orphan",
                "body": "<p>No page on this site links to this one.</p>",
                "links": ["/"], "flags": ["sitemap_only"]},

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

# The filler must be VARIED, not one phrase repeated. A block with only a
# handful of distinct word-shingles breaks simhash outright: the two dominant
# shingles end up with near-equal weights, cancel in every bit column, and let
# a four-shingle difference flip fourteen bits. Real generated pages carry real
# prose around their varying field, so the corpus has to as well or it tests a
# degenerate case that does not occur.
GEN_FILLER_BLOCK = (
    "A crawler is a program that discovers documents by following the links "
    "between them. The frontier holds what has been found and not yet fetched. "
    "Politeness governs how often any single origin may be contacted, and it is "
    "the constraint that separates a useful tool from an outage. Duplicate "
    "detection decides whether two responses are worth storing separately. "
    "Freshness decides when something already stored deserves another look. "
    "Extraction turns markup into text a person or a model can read. Every one "
    "of these is a policy question wearing an engineering costume, and the "
    "answers differ for an archive, a search index and a training corpus."
)

SITEMAPS = {
    "/sitemap.xml": ["/sitemap-1.xml", "/sitemap-2.xml"],   # a sitemap *index*
    "/sitemap-1.xml": ["/", "/a", "/b", "/c"],
    "/sitemap-2.xml": ["/docs/", "/docs/one", "/docs/two", "/docs/sub/three",
                       "/etag", "/orphan"],
}

# Traps a naive crawler must survive, and the stage that fixes each.
TRAPS = {
    "/loop/1": "redirect loop — fixed in stage 5 (normalise + seen-set on redirects)",
    "/gen/1": "unbounded generator — fixed in stage 5 (byte cap + path-depth cap)",
    "/hang": "no response — fixed in stage 4 (per-request timeout)",
}
