"""Polite HTTP client for profession.hu.

Rate-limited and robots.txt-aware on purpose: this project scrapes a site
whose listing database is its commercial product. Fetching slowly, with an
honest User-Agent that names a contact address, is the difference between
"a personal tool checking a public page occasionally" and something that
looks like it's trying to avoid attention. See README for the fuller
rationale.

robots.txt is parsed by hand rather than via stdlib urllib.robotparser,
which gets profession.hu's file wrong in two ways:
  1. It doesn't understand "*" wildcards, and profession.hu's rules are
     mostly wildcards ("Disallow: */jelentkezes/", "Disallow: *?rss$") —
     stdlib treats them as literal prefixes that never match anything.
  2. It applies the first matching rule in file order, not the longest
     (most specific) one as RFC 9309 requires, so a broad Disallow
     listed before a narrower Allow wrongly wins.
"""

import random
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from tracker.config import env, load_yaml

BASE_URL = "https://www.profession.hu"
_ROBOTS_URL = urljoin(BASE_URL, "/robots.txt")

# (allow?, compiled pattern, specificity) — specificity is the rule's
# length, which RFC 9309 uses to pick the most specific matching rule.
_Rule = tuple[bool, re.Pattern, int]


def _rule_regex(rule_path: str) -> re.Pattern:
    """robots.txt path pattern -> regex anchored at the path's start.
    "*" matches any run of characters; a trailing "$" anchors the end.
    """
    anchored = rule_path.endswith("$")
    body = rule_path[:-1] if anchored else rule_path
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def parse_robots_rules(text: str) -> list[_Rule]:
    """Rules from the "User-agent: *" group(s). Our User-Agent string is
    never named specifically in a robots.txt, so that group is the one
    that applies to us.
    """
    rules: list[_Rule] = []
    active = False
    in_agent_lines = False  # consecutive User-agent lines share one group
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()

        if field == "user-agent":
            active = (active and in_agent_lines) or value == "*"
            in_agent_lines = True
            continue
        in_agent_lines = False
        if active and field in ("allow", "disallow") and value:
            rules.append((field == "allow", _rule_regex(value), len(value)))
    return rules


def can_fetch(rules: list[_Rule], url: str) -> bool:
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    best_len, best_allow = -1, True  # no matching rule => allowed
    for allow, pattern, length in rules:
        # On a tie, Allow wins (RFC 9309: "the least restrictive rule").
        if pattern.match(path) and (length > best_len or (length == best_len and allow)):
            best_len, best_allow = length, allow
    return best_allow


class PoliteSession:
    def __init__(self):
        politeness = load_yaml("sources.yaml")["politeness"]
        self.min_delay = politeness["min_delay"]
        self.max_delay = politeness["max_delay"]

        contact = env(politeness["user_agent_contact_env"], required=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            f"profession-hu-tracker/0.1 "
            f"(personal job-search tool; contact: {contact})"
        )

        self._robots_rules = self._load_robots()
        self._last_request_at = 0.0

    def _load_robots(self) -> list[_Rule] | None:
        """None means disallow everything (401/403 on robots.txt itself).
        A missing robots.txt (404) means no restrictions. Any other failure
        raises: crawling blind is worse than skipping one day's run.
        """
        response = self.session.get(_ROBOTS_URL, timeout=15.0)
        if response.status_code in (401, 403):
            return None
        if 400 <= response.status_code < 500:
            return []
        response.raise_for_status()
        return parse_robots_rules(response.text)

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        delay = random.uniform(self.min_delay, self.max_delay)
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, max_retries: int = 3, timeout: float = 15.0) -> requests.Response:
        if self._robots_rules is None or not can_fetch(self._robots_rules, url):
            raise PermissionError(f"robots.txt disallows fetching: {url}")

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            self._throttle()
            try:
                response = self.session.get(url, timeout=timeout)
                self._last_request_at = time.monotonic()
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2**attempt)
                continue

            response.raise_for_status()
            return response

        raise RuntimeError(f"Failed to fetch {url} after {max_retries} retries") from last_exc
