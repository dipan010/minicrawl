"""Stage 3 — robots.txt, parsed by hand.

`urllib.robotparser` exists, and using it would skip the whole lesson. It also
gets the corpus wrong: it does not implement `$` anchoring, its longest-match
handling is not RFC 9309's, and it treats a 500 as "allow all" when the spec
says the opposite.

The four rules that matter, from RFC 9309:

  * §2.3.1.3  4xx (including 404) -> the site has no rules, crawl freely.
  * §2.3.1.4  5xx or unreachable  -> assume a *complete disallow*. This is the
              one everybody gets backwards. A site that is failing is not a
              site that is granting permission.
  * §2.2.2    Allow and Disallow are matched by length, not by order. The
              longest matching pattern wins; on a tie, Allow wins.
  * §2.2.3    `*` matches any run of characters, `$` anchors to end of path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

ALLOW_ALL = "allow_all"
DISALLOW_ALL = "disallow_all"
RULES = "rules"


@dataclass(slots=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern

    @property
    def specificity(self) -> int:
        return len(self.pattern)


@dataclass(slots=True)
class Group:
    agents: list[str]
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def compile_pattern(pattern: str) -> re.Pattern:
    """robots.txt globbing -> regex. Only `*` and a trailing `$` are special."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    out = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + out + ("$" if anchored else ""))


class RobotsTxt:
    def __init__(self, groups: list[Group], sitemaps: list[str], mode: str = RULES):
        self.groups = groups
        self.sitemaps = sitemaps
        self.mode = mode

    # -- construction ------------------------------------------------------
    @classmethod
    def from_response(cls, status: int | None, text: str) -> "RobotsTxt":
        if status is None or status >= 500:
            return cls([], [], mode=DISALLOW_ALL)      # §2.3.1.4 — the trap
        if status >= 400:
            return cls([], [], mode=ALLOW_ALL)         # §2.3.1.3
        return cls.parse(text)

    @classmethod
    def parse(cls, text: str) -> "RobotsTxt":
        groups: list[Group] = []
        sitemaps: list[str] = []
        current: Group | None = None
        starting_group = False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            field_name, value = field_name.strip().lower(), value.strip()

            if field_name == "user-agent":
                # Consecutive User-agent lines share one group of rules.
                if current is None or not starting_group:
                    current = Group(agents=[])
                    groups.append(current)
                    starting_group = True
                current.agents.append(value.lower())
                continue

            starting_group = False
            if field_name == "sitemap":
                sitemaps.append(value)
            elif current is None:
                continue
            elif field_name in ("allow", "disallow"):
                if field_name == "disallow" and value == "":
                    continue                    # "Disallow:" with no value = allow all
                current.rules.append(
                    Rule(allow=field_name == "allow", pattern=value,
                         regex=compile_pattern(value)))
            elif field_name == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass

        return cls(groups, sitemaps)

    # -- querying ----------------------------------------------------------
    def group_for(self, agent: str) -> Group | None:
        """Most specific matching group, else the `*` group, else nothing.

        Matching is on the product token: a group named `minicrawl` applies to
        `minicrawl/0.1 (+https://...)`.
        """
        agent = agent.lower()
        best, best_len = None, -1
        wildcard = None
        for group in self.groups:
            for name in group.agents:
                if name == "*":
                    wildcard = wildcard or group
                elif name in agent and len(name) > best_len:
                    best, best_len = group, len(name)
        return best or wildcard

    def allowed(self, path: str, agent: str) -> bool:
        if self.mode == ALLOW_ALL:
            return True
        if self.mode == DISALLOW_ALL:
            return False
        group = self.group_for(agent)
        if group is None:
            return True

        path = unquote(path) or "/"
        winner: Rule | None = None
        for rule in group.rules:
            if not rule.regex.match(path):
                continue
            # Longest pattern wins; Allow breaks a tie (§2.2.2).
            if (winner is None
                    or rule.specificity > winner.specificity
                    or (rule.specificity == winner.specificity and rule.allow)):
                winner = rule
        return True if winner is None else winner.allow

    def crawl_delay(self, agent: str) -> float | None:
        group = self.group_for(agent)
        return group.crawl_delay if group else None


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"
