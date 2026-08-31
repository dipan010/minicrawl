"""Serves the synthetic corpus declared in spec.py on four local origins.

    uv run python -m testsite.server            # blocks, serves 8081-8084
    uv run python -m testsite.server --once /a  # print one page and exit

Four origins exist so that per-host politeness (stage 4) and host-sharded
workers (stage 9) have something to actually demonstrate. Politeness keys on
host:port, so 127.0.0.1:8081 and 127.0.0.1:8082 are different hosts.
"""
from __future__ import annotations

import html
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import spec

ETAG = '"v1-etag"'
LAST_MODIFIED = "Wed, 12 Feb 2025 10:00:00 GMT"


def variant_hrefs(page: dict) -> list[str]:
    """Every spelling on /variants, in one flat list, malformed ones included."""
    out = [h for g in page["variant_groups"].values() for h in g["hrefs"]]
    return out + list(page["redirect_normalized"]) + page["malformed_hrefs"]


def render_page(path: str, port: int) -> str:
    page = spec.PAGES[path]
    title = page.get("title", path)
    head = [f"<title>{html.escape(title)}</title>"]
    if "base" in page:
        head.append(f'<base href="{page["base"].format(port=port)}">')
    if "canonical" in page:
        head.append(f'<link rel="canonical" href="http://127.0.0.1:{port}{page["canonical"]}">')

    parts = [page.get("body", "")]

    if path == "/variants":
        parts.append("<ul>")
        for href in variant_hrefs(page):
            parts.append(f'<li><a href="{html.escape(href)}">{html.escape(href)}</a></li>')
        parts.append("</ul>")
    elif "raw_html" in page:
        parts.append(page["raw_html"])

    raw = page.get("raw", {})
    hrefs = [raw.get(t, t) for t in page.get("links", [])]
    if page.get("cross_host"):
        hrefs += [f"http://{spec.host(p)}/" for p in spec.HOST_PORTS if p != port]
    if hrefs:
        parts.append("<nav>" + " ".join(
            f'<a href="{html.escape(h)}">{html.escape(h)}</a>' for h in hrefs) + "</nav>")

    return (f"<!doctype html><html><head><meta charset=utf-8>{''.join(head)}</head>"
            f"<body><h1>{html.escape(title)}</h1>{''.join(parts)}</body></html>")


def render_sitemap(path: str, port: int) -> str:
    entries = spec.SITEMAPS[path]
    base = f"http://{spec.host(port)}"
    if path == "/sitemap.xml":
        body = "".join(f"<sitemap><loc>{base}{e}</loc></sitemap>" for e in entries)
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"{body}</sitemapindex>")
    body = "".join(
        f"<url><loc>{base}{e}</loc><lastmod>2025-02-12</lastmod></url>" for e in entries)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "minicrawl-testsite/1.0"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the crawl output readable
        if self.server.verbose:
            sys.stderr.write("  testsite %s %s\n" % (self.server.port, fmt % args))

    def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        port = self.server.port
        path, _, query = self.path.partition("?")

        if path == "/robots.txt":
            r = spec.ROBOTS[port]
            return self.send(r["status"], r["body"].encode(), "text/plain; charset=utf-8")

        if path in spec.SITEMAPS:
            return self.send(200, render_sitemap(path, port).encode(), "application/xml")

        if path.startswith(spec.GEN_PREFIX):
            return self.gen(path, port)

        # A trailing-slash spelling is not silently served: the server answers
        # with a 301, the way a real one does. That is how a crawler is supposed
        # to learn that /a/ and /a are the same page -- not by guessing.
        if path not in spec.PAGES and path.endswith("/") and path[:-1] in spec.PAGES:
            self.send_response(301)
            self.send_header("Location", path[:-1])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        key = path
        page = spec.PAGES.get(key)
        if page is None:
            return self.send(404, b"<h1>404</h1>", "text/html; charset=utf-8")

        if (delay := page.get("delay")):
            time.sleep(delay)

        if "redirect" in page:
            self.send_response(page.get("status", 302))
            self.send_header("Location", page["redirect"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if "raw_bytes" in page:
            return self.send(200, page["raw_bytes"], page["content_type"])

        if page.get("etag"):
            if self.headers.get("If-None-Match") == ETAG:
                self.send_response(304)
                self.send_header("ETag", ETAG)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = render_page(key, port).encode()
            return self.send(200, body, "text/html; charset=utf-8",
                             {"ETag": ETAG, "Last-Modified": LAST_MODIFIED})

        self.send(200, render_page(key, port).encode(), "text/html; charset=utf-8")

    def gen(self, path: str, port: int):
        """An unbounded chain of fat pages. A crawler with no caps never finishes."""
        try:
            n = int(path[len(spec.GEN_PREFIX):].strip("/"))
        except ValueError:
            return self.send(404, b"<h1>404</h1>", "text/html; charset=utf-8")
        paragraph = "<p>%s</p>" % ("generated filler " * 40)
        filler = paragraph * (spec.GEN_BODY_BYTES // len(paragraph) + 1)
        body = (f"<!doctype html><html><head><title>Generated {n}</title></head><body>"
                f"<h1>Generated {n}</h1>{filler}"
                f'<nav><a href="{spec.GEN_PREFIX}{n + 1}">next</a>'
                f'<a href="{spec.GEN_PREFIX}{n * 2 + 1}">branch</a></nav>'
                "</body></html>")
        self.send(200, body.encode(), "text/html; charset=utf-8")


def serve(ports=spec.HOST_PORTS, verbose=False):
    servers = []
    for port in ports:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        httpd.port, httpd.verbose = port, verbose
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
    return servers


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(render_page(sys.argv[sys.argv.index("--once") + 1], spec.PRIMARY))
        raise SystemExit
    serve(verbose="-v" in sys.argv)
    print(f"testsite on {', '.join('http://' + spec.host(p) for p in spec.HOST_PORTS)}")
    print("ctrl-c to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
