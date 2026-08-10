"""Polite, robots.txt-aware HTTP client shared by every source adapter.

Rate-limited and robots.txt-aware on purpose: several of these sites'
listing databases are their commercial product. Fetching slowly, with an
honest User-Agent that names a contact address, is the difference between
"a personal tool checking a public page occasionally" and something that
looks like it's trying to avoid attention. See README for the fuller
rationale.

Each host gets its own PoliteSession (its own robots.txt, its own
throttle clock) — a session fetched for one host must never be reused to
fetch a different one, since robots.txt is loaded once at construction
time for that host specifically.

robots.txt is parsed by hand rather than via stdlib's
urllib.robotparser, for two confirmed reasons:
  1. RobotFileParser.read() fetches through Python's default urllib
     User-Agent internally, not our own — Eightfold's CDN 403s that
     generic UA even though it explicitly allows ours.
  2. RobotFileParser matches rules in file order and stops at the first
     hit, not "longest matching path wins." The extremely common
     "Disallow: / then Allow: /specific/path" idiom (used by every
     Eightfold-hosted site) breaks under that: the leading blanket
     Disallow matches first and wins, regardless of the more specific
     Allow listed after it — silently blocking everything despite the
     site granting access.
Both were caught by testing against real Eightfold responses, not
theory.
"""

import random
import time
from urllib.parse import urljoin, urlsplit

import requests

from tracker.config import env, load_yaml

BASE_URL = "https://www.profession.hu"  # profession.hu's own crawler still uses this default


def _parse_robots_rules(text: str) -> list[tuple[str, str, bool]]:
    """Rules for the User-agent: * group, as (directive, path, exact) triples.

    Doesn't support mid-path "*" wildcards — not needed by anything
    checked so far. The "$" end-of-path anchor is handled.
    """
    rules: list[tuple[str, str, bool]] = []
    active = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()

        if field == "user-agent":
            active = value == "*"
        elif active and field in ("allow", "disallow") and value:
            exact = value.endswith("$")
            rules.append((field, value[:-1] if exact else value, exact))

    return rules


def _can_fetch(rules: list[tuple[str, str, bool]], path: str) -> bool:
    """Longest matching rule wins (RFC 9309), not first-in-file-order."""
    best_len, best_allow = -1, True  # no matching rule => allowed
    for directive, rule_path, exact in rules:
        matches = path == rule_path if exact else path.startswith(rule_path)
        if matches and len(rule_path) > best_len:
            best_len, best_allow = len(rule_path), directive == "allow"
    return best_allow


class PoliteSession:
    def __init__(self, base_url: str = BASE_URL):
        politeness = load_yaml("politeness.yaml")
        self.min_delay = politeness["min_delay"]
        self.max_delay = politeness["max_delay"]

        contact = env(politeness["user_agent_contact_env"], required=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            f"job-tracker/0.1 (personal job-search tool; contact: {contact})"
        )

        self._robots_rules = self._load_robots(urljoin(base_url, "/robots.txt"))
        self._last_request_at = 0.0

    def _load_robots(self, robots_url: str) -> list[tuple[str, str, bool]] | None:
        """None means disallow everything (401/403 on robots.txt itself).
        A missing/erroring robots.txt (404, 5xx, network failure) yields
        an empty rule list, which fails open — same convention as "no
        robots.txt present" meaning no restrictions.
        """
        try:
            response = self.session.get(robots_url, timeout=15.0)
        except requests.RequestException:
            return []
        if response.status_code in (401, 403):
            return None
        if response.status_code == 200:
            return _parse_robots_rules(response.text)
        return []

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        delay = random.uniform(self.min_delay, self.max_delay)
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _request(
        self, method: str, url: str, max_retries: int = 3, timeout: float = 15.0, **kwargs
    ) -> requests.Response:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if self._robots_rules is None or not _can_fetch(self._robots_rules, path):
            raise PermissionError(f"robots.txt disallows fetching: {url}")

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            self._throttle()
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
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

        raise RuntimeError(f"Failed to {method} {url} after {max_retries} retries") from last_exc

    def get(self, url: str, max_retries: int = 3, timeout: float = 15.0) -> requests.Response:
        return self._request("GET", url, max_retries=max_retries, timeout=timeout)

    def post(self, url: str, max_retries: int = 3, timeout: float = 15.0, **kwargs) -> requests.Response:
        return self._request("POST", url, max_retries=max_retries, timeout=timeout, **kwargs)
