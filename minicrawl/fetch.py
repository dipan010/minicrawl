"""Stage 1 — one HTTP request, honestly reported.

The only interesting decisions here are the ones that bite later: cap the body
before you read it, record the *final* URL after redirects, and never let a
single slow host stall the crawl (see /hang in the test corpus).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

DEFAULT_UA = "minicrawl/0.1 (+https://example.invalid/minicrawl; learning project)"
MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class Fetched:
    url: str                        # what we asked for
    final_url: str                  # where we ended up after redirects
    status: int | None
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0
    error: str | None = None
    redirects: list[str] = field(default_factory=list)
    cap: int = MAX_BODY_BYTES       # the byte cap this fetch was made under

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        """304: the server declined to send a body because our validator matched.

        Not an error and not a success — a third outcome, and the cheapest
        possible answer to "has this changed?". Treating it as either of the
        other two is how conditional GET gets quietly broken."""
        return self.status == 304

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type

    @property
    def truncated(self) -> bool:
        return len(self.body) >= self.cap


def make_client(timeout: float = 10.0, user_agent: str = DEFAULT_UA) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=httpx.Timeout(timeout, connect=5.0),
        follow_redirects=True,
        max_redirects=5,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )


async def fetch(client: httpx.AsyncClient, url: str,
                max_bytes: int = MAX_BODY_BYTES,
                headers: dict[str, str] | None = None) -> Fetched:
    started = time.perf_counter()
    try:
        # Streaming lets us stop reading a body that turns out to be enormous.
        async with client.stream("GET", url, headers=headers) as resp:
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            return Fetched(
                url=url,
                final_url=str(resp.url),
                status=resp.status_code,
                content_type=resp.headers.get("content-type", "").split(";")[0].strip(),
                body=b"".join(chunks)[:max_bytes],
                headers={k.lower(): v for k, v in resp.headers.items()},
                elapsed=time.perf_counter() - started,
                redirects=[str(r.url) for r in resp.history],
                cap=max_bytes,
            )
    except httpx.TooManyRedirects:
        return _failed(url, "redirect_loop", started)
    except httpx.TimeoutException:
        return _failed(url, "timeout", started)
    except httpx.HTTPError as exc:
        return _failed(url, f"{type(exc).__name__}: {exc}", started)


def _failed(url: str, error: str, started: float) -> Fetched:
    return Fetched(url=url, final_url=url, status=None, content_type="", body=b"",
                   elapsed=time.perf_counter() - started, error=error)
