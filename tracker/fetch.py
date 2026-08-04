"""Polite HTTP client for profession.hu.

Rate-limited and robots.txt-aware on purpose: this project scrapes a site
whose listing database is its commercial product. Fetching slowly, with an
honest User-Agent that names a contact address, is the difference between
"a personal tool checking a public page occasionally" and something that
looks like it's trying to avoid attention. See README for the fuller
rationale.
"""

import random
import time
import urllib.robotparser
from urllib.parse import urljoin

import requests

from tracker.config import env, load_yaml

BASE_URL = "https://www.profession.hu"
_ROBOTS_URL = urljoin(BASE_URL, "/robots.txt")


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

        self._robots = urllib.robotparser.RobotFileParser()
        self._robots.set_url(_ROBOTS_URL)
        self._robots.read()

        self._last_request_at = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        delay = random.uniform(self.min_delay, self.max_delay)
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, max_retries: int = 3, timeout: float = 15.0) -> requests.Response:
        if not self._robots.can_fetch(self.session.headers["User-Agent"], url):
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
