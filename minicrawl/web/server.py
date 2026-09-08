"""Stage 13 — a front end, and what a real site does to a crawler.

WHY THE CRAWL CANNOT LIVE IN THE BROWSER

The obvious design is a page that fetches URLs itself. It cannot work, and the
reason is worth stating because it is the same reason crawlers are servers:

  A browser will not let a page read another origin's HTML. Cross-origin
  reads are blocked unless the target opts in with CORS headers, and no site
  on the web opts in for arbitrary readers. A page can *request* a URL; it
  cannot see the response.

So the crawl runs in Python and the browser is a VIEW onto it. That split is
not a limitation of this project — it is why every crawler you have ever used
is a server process.

WHY THERE IS NO WEB FRAMEWORK HERE

Two GET routes, no request bodies, no sessions, no templating. Flask, FastAPI
and aiohttp would each be more code to install than the thing they replace.
`asyncio.start_server` plus about a hundred lines of HTTP is honest at this
size, and it keeps the core install at two dependencies.

SERVER-SENT EVENTS

The crawl streams. SSE is the protocol for that when the traffic only goes one
way: a normal HTTP response that never ends, with each message written as

    data: {"...": ...}\\n\\n

The blank line is the message terminator; omit it and the browser waits
forever without erroring — the same silent failure this project hit at stage
10, where two Scrapy hooks did nothing and reported success.
"""
from __future__ import annotations

import asyncio
import json
import socket
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..crawler import CrawlConfig, Page, crawl
from ..dedup import DuplicateIndex
from ..reader import RobotsGate, read_one
from ..fetch import make_client
from ..traps import TrapGuard
from .policy import LOCAL, Policy, from_env

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

# Kept as module constants because the tests and the CLI both read them; the
# authority on what a given deployment allows is a Policy, not these.
MAX_PAGES_CEILING = LOCAL.max_pages
MAX_WORKERS_CEILING = LOCAL.max_workers
MIN_DELAY = LOCAL.min_delay


def page_event(page: Page) -> dict:
    """The wire format for one crawled page.

    Written out field by field rather than `asdict(page)`: adding a field to
    `Page` in a later stage must not silently change what the browser reads.
    """
    return {
        "url": page.url,
        "final_url": page.final_url,
        "status": page.status,
        "depth": page.depth,
        "title": page.title,
        "links": page.n_links,
        "elapsed": round(page.elapsed, 3),
        "error": page.error,
        "verdict": page.verdict,
        "duplicate_of": page.duplicate_of,
        "words": page.words,
        "from_cache": page.from_cache,
        "host": urlsplit(page.final_url or page.url).netloc,
    }


def summary_event(result) -> dict:
    """The numbers `cli.report()` prints, as JSON."""
    return {
        "pages": len(result.pages),
        "errors": len(result.errors),
        "blocked_by_robots": result.blocked_by_robots,
        "duration": round(result.duration, 2),
        "stopped_because": result.stopped_because,
        "workers": result.workers,
        "hosts_seen": result.hosts_seen,
        "peak_in_flight": result.peak_in_flight,
        "peak_in_flight_per_host": result.peak_in_flight_per_host,
        "dedup_counts": result.dedup_counts,
        "rejected_by_traps": result.rejected_by_traps,
        "bytes_downloaded": result.bytes_downloaded,
        "worker_seconds_waiting": round(result.worker_seconds_waiting, 1),
    }


# --- the crawl, as a stream of events --------------------------------------

async def crawl_events(seed: str, max_pages: int, max_depth: int, workers: int,
                       delay: float, same_host: bool, policy: Policy = LOCAL):
    """Run a crawl, yielding an event dict per page and a final summary.

    `on_page` is SYNCHRONOUS — `crawler._emit` calls it directly, and making it
    a coroutine would change that contract for every stage behind it. So the
    callback only does `put_nowait` onto a queue, and this generator drains it.
    Exactly the split `push`/`push_nowait` made at stage 4, for the same reason:
    the producer is not allowed to await.
    """
    queue: asyncio.Queue = asyncio.Queue()
    pages, workers, delay = policy.clamp(max_pages, workers, delay)
    config = CrawlConfig(
        seeds=[seed],
        max_pages=pages,
        max_depth=max_depth,
        workers=workers,
        default_delay=delay,
        same_host=same_host,
        # respect_robots is NOT configurable from the browser. The CLI exposes
        # --ignore-robots because a human running it owns the consequences; a
        # form on a web page does not get that option.
        respect_robots=True,
        traps=TrapGuard(),
        dedup=DuplicateIndex(),
        on_page=queue.put_nowait,
    )

    task = asyncio.create_task(crawl(config))
    try:
        while True:
            drain = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({drain, task},
                                         return_when=asyncio.FIRST_COMPLETED)
            if drain in done:
                yield "page", page_event(drain.result())
                continue
            drain.cancel()
            # The crawl finished. Anything still queued was produced before it
            # returned and belongs to this crawl, so flush before closing.
            while not queue.empty():
                yield "page", page_event(queue.get_nowait())
            yield "done", summary_event(await task)
            return
    finally:
        # Closing the tab, or pressing Stop, closes this generator. Without
        # this the crawl would keep running with nobody watching — and keep
        # making requests to someone else's site. A viewer going away is a
        # reason to stop, not a reason to carry on quietly.
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass


# --- the smallest HTTP server that can do this -----------------------------

def _response(status: str, content_type: str, body: bytes,
              extra: str = "") -> bytes:
    return (f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{extra}Connection: close\r\n\r\n").encode() + body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        request = await asyncio.wait_for(reader.readline(), timeout=10)
    except (asyncio.TimeoutError, ConnectionError):
        writer.close()
        return
    if not request:
        writer.close()
        return

    try:
        method, target, _ = request.decode("latin-1").split(" ", 2)
    except ValueError:
        writer.write(_response("400 Bad Request", "text/plain", b"bad request"))
        await writer.drain()
        writer.close()
        return

    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()

    path, _, query = target.partition("?")
    policy = SERVER_POLICY
    try:
        if method != "GET":
            writer.write(_response("405 Method Not Allowed", "text/plain",
                                   b"GET only"))
        elif path == "/":
            writer.write(_response("200 OK", "text/html; charset=utf-8",
                                   INDEX.read_bytes()))
        elif path == "/config":
            # The page asks what this deployment allows, rather than shipping
            # two hard-coded variants of itself.
            writer.write(_response("200 OK", "application/json",
                                   json.dumps(policy.describe()).encode()))
        elif path == "/read":
            await _read(writer, parse_qs(query), policy, client_of(writer, headers))
        elif path == "/healthz":
            writer.write(_response("200 OK", "text/plain", b"ok"))
        elif path == "/crawl":
            await _stream_crawl(writer, parse_qs(query),
                                policy, client_of(writer, headers))
            return
        else:
            writer.write(_response("404 Not Found", "text/plain", b"not found"))
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def _send(writer: asyncio.StreamWriter, event: str, data: dict) -> None:
    """One SSE message. The trailing BLANK LINE is the terminator — without it
    the browser buffers the message forever and reports nothing wrong."""
    payload = json.dumps(data)
    writer.write(f"event: {event}\ndata: {payload}\n\n".encode())
    await writer.drain()


async def _read(writer: asyncio.StreamWriter, params: dict,
                policy: Policy, client: str) -> None:
    """GET /read?url=... — one page as Markdown, in one round trip.

    The same policy governs it as a crawl: a hosted instance must not fetch
    its own network just because the endpoint is smaller.
    """
    seed = unquote((params.get("url") or [""])[0]).strip()
    refusal = policy.check_seed(seed)
    if refusal:
        body = json.dumps({"url": seed, "error": refusal}).encode()
        writer.write(_response("400 Bad Request", "application/json", body))
        return
    async with make_client(timeout=15.0) as http:
        # A RobotsGate is not optional here. `read_one` defaults it to None,
        # which is right for a library call the caller controls and wrong for
        # an endpoint on a public host: stage 14's promise is that robots is
        # always obeyed and cannot be switched off from a form, and a smaller
        # endpoint does not get an exemption from it.
        result = await read_one(http, seed, robots=RobotsGate(http))
    body = json.dumps(result.to_dict(), ensure_ascii=False).encode()
    status = "200 OK" if result.ok else "502 Bad Gateway"
    writer.write(_response(status, "application/json; charset=utf-8", body))


def client_of(writer: asyncio.StreamWriter, headers: dict) -> str:
    """Who is asking, for rate limiting.

    Behind a platform proxy every connection appears to come from the proxy, so
    the first entry of X-Forwarded-For is the visitor. That header is
    trivially spoofable and is therefore used ONLY to spread rate limits over
    visitors, never to decide access.
    """
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    peer = writer.get_extra_info("peername")
    return peer[0] if peer else "unknown"


async def _stream_crawl(writer: asyncio.StreamWriter, params: dict,
                        policy: Policy = LOCAL, client: str = "local") -> None:
    seed = unquote((params.get("url") or [""])[0]).strip()

    def number(name, default, cast=int):
        try:
            return cast((params.get(name) or [default])[0])
        except (TypeError, ValueError):
            return default

    # No Content-Length: the body is open-ended. `X-Accel-Buffering` tells a
    # proxy not to hold the stream back, which is the other classic way SSE
    # arrives all at once at the end or not at all.
    writer.write(b"HTTP/1.1 200 OK\r\n"
                 b"Content-Type: text/event-stream; charset=utf-8\r\n"
                 b"Cache-Control: no-cache\r\n"
                 b"X-Accel-Buffering: no\r\n"
                 b"Connection: close\r\n\r\n")
    await writer.drain()

    refusal = policy.check_seed(seed) or policy.check_rate(client)
    try:
        if refusal:
            await _send(writer, "failed", {"error": refusal})
        else:
            policy.begin(client)
            try:
                await _send(writer, "started", {"seed": seed})
                async for kind, data in crawl_events(
                        seed, number("max_pages", 40), number("max_depth", 3),
                        number("workers", 4), number("delay", 0.5, float),
                        (params.get("same_host") or ["1"])[0] != "0", policy):
                    await _send(writer, kind, data)
            finally:
                policy.end(client)
    except (ConnectionResetError, BrokenPipeError):
        return                                   # the tab was closed
    except Exception as exc:                     # noqa: BLE001
        # A real site can break the crawler in ways the corpus never did. Say
        # so in the UI instead of dying silently behind a spinner.
        await _send(writer, "failed", {"error": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            writer.close()
        except Exception:                        # noqa: BLE001
            pass


def _acceptable(seed: str) -> bool:
    """Scheme check only — the full decision belongs to a Policy."""
    return LOCAL.check_seed(seed) is None


SERVER_POLICY: Policy = LOCAL


async def serve(host: str = "127.0.0.1", port: int = 8000,
                open_browser: bool = True, policy: Policy | None = None) -> None:
    global SERVER_POLICY
    SERVER_POLICY = policy or from_env()
    server = await asyncio.start_server(_handle, host, port)
    url = f"http://{host}:{port}/"
    print(f"minicrawl web ui on {url}")
    print("  the crawl runs here in Python; the page only watches it")
    if SERVER_POLICY.public:
        print(f"  PUBLIC policy: {len(SERVER_POLICY.allowlist)} allowed sites, "
              f"max {SERVER_POLICY.max_pages} pages, "
              f"{SERVER_POLICY.max_concurrent} concurrent crawls")
    if open_browser:
        webbrowser.open(url)
    async with server:
        await server.serve_forever()


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]
