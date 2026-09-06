"""Stage 14 — the difference between running a crawler and exposing one.

On your own machine, a box that crawls any URL you type is a tool. On the
public internet, the same box is a request-forging service with someone else's
name on the bill, and the requests leave from the host's IP with the operator's
account attached.

Three things change, and none of them are optional:

SSRF.  "Fetch this URL for me" pointed inward is one of the oldest holes there
       is. `http://169.254.169.254/` is the cloud metadata endpoint: on most
       providers it hands out the instance's credentials to anything that asks
       from inside. `http://127.0.0.1:6379/` is whatever else the box is
       running. The seed's hostname is therefore RESOLVED and every address it
       maps to is checked, because `localtest.me` and friends are public names
       that resolve to 127.0.0.1 — checking the string would catch nothing.

ABUSE. A stranger can start crawls faster than the crawler finishes them.
       Without a cap, the host becomes a free amplifier pointed at whoever the
       stranger chooses, and the complaint arrives at the operator.

SCOPE. The safest public demo does not accept arbitrary targets at all. An
       allowlist of sites that exist to be crawled shows exactly the same
       machinery and cannot be turned into a weapon.

Locally none of this applies: `LOCAL` allows loopback, because the whole test
corpus lives at 127.0.0.1 and the person typing owns the consequences.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Sites that exist to be crawled, plus this project's own report. Nothing here
# is a stranger's server being used without their knowledge.
DEMO_SITES = (
    ("https://quotes.toscrape.com/", "Quotes to Scrape — pagination, tags, near-duplicates"),
    ("https://books.toscrape.com/", "Books to Scrape — 1,000 pages, deep catalogue"),
    ("https://dipan010.github.io/minicrawl/", "This project's own report page"),
    ("https://example.com/", "example.com — one page, the smallest possible crawl"),
)


@dataclass
class Policy:
    """What a visitor is allowed to ask for."""
    allowlist: tuple[str, ...] = ()        # exact seed URLs; empty = any public host
    allow_private: bool = True             # loopback and RFC1918
    max_pages: int = 300
    max_workers: int = 8
    min_delay: float = 0.5
    max_concurrent: int = 4                # crawls running at once, whole server
    per_ip_cooldown: float = 0.0           # seconds between one visitor's crawls
    public: bool = False

    _running: int = field(default=0, repr=False)
    _last_seen: dict = field(default_factory=dict, repr=False)

    # -- what may be crawled ----------------------------------------------
    def check_seed(self, seed: str) -> str | None:
        """None if the seed is allowed, else the reason to show the visitor."""
        parts = urlsplit(seed)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "Enter an http:// or https:// URL."

        if self.allowlist:
            if seed.rstrip("/") not in {a.rstrip("/") for a in self.allowlist}:
                return "This demo crawls a fixed list of sites. Pick one from the list."
            return None

        if not self.allow_private and not self._resolves_public(parts.hostname):
            # Deliberately vague: a precise answer here is a free port scanner,
            # since "blocked" and "no such host" would map the private network.
            return "That address cannot be crawled from here."
        return None

    def _resolves_public(self, host: str) -> bool:
        """Every address the name resolves to must be publicly routable.

        A name is checked, not a string. `localtest.me` is a perfectly public
        hostname that resolves to 127.0.0.1, and a substring check for
        "localhost" would wave it straight through.
        """
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return False
        if not infos:
            return False
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            # is_global excludes loopback, link-local (169.254.x), RFC1918,
            # multicast, reserved and the unspecified address in one predicate.
            if not address.is_global:
                return False
        return True

    # -- how often ---------------------------------------------------------
    def check_rate(self, client: str) -> str | None:
        if self._running >= self.max_concurrent:
            return "The demo is busy — a few crawls are already running. Try again in a moment."
        if self.per_ip_cooldown:
            waited = time.monotonic() - self._last_seen.get(client, 0.0)
            if waited < self.per_ip_cooldown:
                return (f"One crawl at a time, please — "
                        f"{self.per_ip_cooldown - waited:.0f}s to go.")
        return None

    def begin(self, client: str) -> None:
        self._running += 1
        self._last_seen[client] = time.monotonic()

    def end(self, client: str) -> None:
        self._running = max(0, self._running - 1)
        self._last_seen[client] = time.monotonic()

    # -- clamping ----------------------------------------------------------
    def clamp(self, pages: int, workers: int, delay: float) -> tuple[int, int, float]:
        return (max(1, min(pages, self.max_pages)),
                max(1, min(workers, self.max_workers)),
                max(delay, self.min_delay))

    def describe(self) -> dict:
        """What the page needs to render itself correctly."""
        return {"public": self.public,
                "sites": [{"url": u, "label": l} for u, l in DEMO_SITES]
                         if self.allowlist else [],
                "max_pages": self.max_pages, "max_workers": self.max_workers,
                "min_delay": self.min_delay}


#: On your own machine: the corpus is at 127.0.0.1, and you own the target.
LOCAL = Policy(allow_private=True, public=False)

#: On the internet: a fixed list, modest caps, one crawl per visitor at a time.
PUBLIC = Policy(allowlist=tuple(url for url, _ in DEMO_SITES),
                allow_private=False, max_pages=40, max_workers=4,
                min_delay=1.0, max_concurrent=3, per_ip_cooldown=20.0,
                public=True)


def from_env() -> Policy:
    """PUBLIC only when something explicitly asks for it. A deployment that
    forgets the flag gets the safe policy, not the permissive one."""
    return PUBLIC if os.environ.get("MINICRAWL_PUBLIC") == "1" else LOCAL
