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
from ..traps import TrapGuard

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

# Caps the form cannot raise. A UI that lets a stranger point this at someone
# else's site is a UI that needs limits the CLI can leave to judgement.
MAX_PAGES_CEILING = 300
MAX_WORKERS_CEILING = 8
MIN_DELAY = 0.5          # seconds between requests to one host


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
                       delay: float, same_host: bool):
    """Run a crawl, yielding an event dict per page and a final summary.

    `on_page` is SYNCHRONOUS — `crawler._emit` calls it directly, and making it
    a coroutine would change that contract for every stage behind it. So the
    callback only does `put_nowait` onto a queue, and this generator drains it.
    Exactly the split `push`/`push_nowait` made at stage 4, for the same reason:
    the producer is not allowed to await.
    """
    queue: asyncio.Queue = asyncio.Queue()
    config = CrawlConfig(
        seeds=[seed],
        max_pages=min(max_pages, MAX_PAGES_CEILING),
        max_depth=max_depth,
        workers=min(workers, MAX_WORKERS_CEILING),
        default_delay=max(delay, MIN_DELAY),
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

    while True:                                  # discard headers
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break

    path, _, query = target.partition("?")
    try:
        if method != "GET":
            writer.write(_response("405 Method Not Allowed", "text/plain",
                                   b"GET only"))
        elif path == "/":
            writer.write(_response("200 OK", "text/html; charset=utf-8",
                                   INDEX.read_bytes()))
        elif path == "/crawl":
            await _stream_crawl(writer, parse_qs(query))
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


async def _stream_crawl(writer: asyncio.StreamWriter, params: dict) -> None:
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

    try:
        if not _acceptable(seed):
            await _send(writer, "failed",
                        {"error": "Enter an http:// or https:// URL."})
        else:
            await _send(writer, "started", {"seed": seed})
            async for kind, data in crawl_events(
                    seed, number("max_pages", 40), number("max_depth", 3),
                    number("workers", 4), number("delay", 0.5, float),
                    (params.get("same_host") or ["1"])[0] != "0"):
                await _send(writer, kind, data)
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
    parts = urlsplit(seed)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


async def serve(host: str = "127.0.0.1", port: int = 8000,
                open_browser: bool = True) -> None:
    server = await asyncio.start_server(_handle, host, port)
    url = f"http://{host}:{port}/"
    print(f"minicrawl web ui on {url}")
    print("  the crawl runs here in Python; the page only watches it")
    if open_browser:
        webbrowser.open(url)
    async with server:
        await server.serve_forever()


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]
