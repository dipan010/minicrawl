"""Stage 7 — rendering the pages that need it, and only those.

A headless browser costs 10-50x what an HTTP request costs, in wall time, CPU
and memory. Rendering every page is the obvious implementation and the wrong
one: on a typical site well over ninety percent of pages are fully present in
the HTTP response, and paying browser prices for all of them buys nothing.

So the interesting work is not the rendering. It is the *triage*: deciding,
from the cheap response you already have, whether a browser would tell you
anything new. Get that wrong in one direction and you miss content; wrong in
the other and the crawl costs an order of magnitude more than it should.

The signals below are all computed from markup already parsed, so triage is
free. None of them is conclusive alone — a short page is not necessarily a
shell, and a script-heavy page may still have all its content — so they are
combined as rules with stated reasons rather than as an opaque score. When a
crawl escalates the wrong page, you want to know which rule fired.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .extract import Extracted

# A shell page has almost no text. A real page with little text (a stub, an
# index) is common too, which is why this never fires on its own.
FEW_WORDS = 25
# Scripts outweighing prose is what a client-rendered page looks like on the wire.
SCRIPT_HEAVY_RATIO = 0.30


@dataclass(slots=True)
class Triage:
    should_render: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.should_render


def triage(found: Extracted, body_bytes: int) -> Triage:
    """Decide whether a browser would add anything, using only what we have."""
    reasons: list[str] = []
    words = len(found.main_text.split())
    script_share = found.script_bytes / body_bytes if body_bytes else 0.0

    # 1. An empty framework mount point. Nearly conclusive on its own: the
    #    markup literally says "something will be inserted here".
    if found.empty_app_root:
        reasons.append("empty_app_root")

    # 2. The page says so itself. Rare, cheap, and unambiguous.
    if found.noscript_hint:
        reasons.append("noscript_hint")

    # 3. Little text AND heavy scripting. Neither half is enough: /docs/one is
    #    three words and needs no browser; a documentation page can be script
    #    heavy and still fully rendered server-side.
    if words < FEW_WORDS and script_share > SCRIPT_HEAVY_RATIO:
        reasons.append("thin_and_script_heavy")

    # 4. A page with text but no links at all is often a shell whose navigation
    #    has not been built yet. Weak, so it needs a script present too.
    if not found.links and found.script_bytes and words < FEW_WORDS:
        reasons.append("no_links_with_scripts")

    return Triage(should_render=bool(reasons), reasons=reasons)


class Renderer(Protocol):
    """Anything that can turn a URL into post-JavaScript HTML."""

    async def render(self, url: str) -> str | None: ...

    async def close(self) -> None: ...


class PlaywrightRenderer:
    """A single headless Chromium, reused for the whole crawl.

    Launching a browser costs roughly a second; launching one per page would
    make the escalation cost dwarf the fetch it is replacing. One browser, one
    page per render, closed after.

    Rendering happens while the crawler still holds that host's in-flight slot,
    so a render's subresource requests cannot overlap another crawl request to
    the same host. Politeness survives the escalation without extra machinery.
    """

    def __init__(self, timeout: float = 10.0, wait_until: str = "networkidle"):
        self.timeout = timeout
        self.wait_until = wait_until
        self._playwright = None
        self._browser = None
        self.rendered = 0
        self.failed = 0

    async def _browser_ready(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def render(self, url: str) -> str | None:
        try:
            browser = await self._browser_ready()
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until=self.wait_until,
                                timeout=self.timeout * 1000)
                html = await page.content()
            finally:
                await page.close()
            self.rendered += 1
            return html
        except Exception:
            # A render failure must never end a crawl. The unrendered response
            # is still a result, and is what we would have had anyway.
            self.failed += 1
            return None

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def playwright_available() -> bool:
    """True if the module AND a downloaded browser are both present.

    Deliberately filesystem-only. Asking Playwright itself means starting its
    driver process, which costs about a second and leaves a noisy teardown
    warning behind — far too much for a question that only gates a test skip.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    roots = ([Path(override)] if override else [
        Path.home() / "Library" / "Caches" / "ms-playwright",       # macOS
        Path.home() / ".cache" / "ms-playwright",                   # Linux
        Path(os.environ.get("LOCALAPPDATA", "~")) / "ms-playwright",  # Windows
    ])
    return any(root.is_dir() and any(root.glob("chromium*")) for root in roots)
