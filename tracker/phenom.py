"""Phenom adapter — covers companies in config/companies_phenom.yaml
(GE HealthCare).

List data is embedded directly in the search page's own JS, under a key
literally named "eagerLoadRefineSearch" — found by grepping for the
"jobs":[ array first, then working backward to its actual enclosing key
(an earlier grep of the surrounding text was truncated mid-word and
looked like "refineSearch", which turned out to be a red herring: a
short, unrelated key that happens to share a suffix).

Detail pages use standard schema.org JobPosting JSON-LD — the exact
same shape profession.hu uses — so tracker/detail_parser.py's existing
parser is reused directly rather than writing a new one. Phenom's
version double-HTML-escapes the description (literal "&lt;h1&gt;" text,
not real tags), so it needs an unescape pass before stripping tags,
unlike profession.hu's already-clean text.

Detail URLs use `jobSeqNo` (e.g. "GEVGHLGLOBALR4043995EXTERNALENGLOBAL"),
not the shorter `jobId`/`reqId` — confirmed against real sitemap.xml
entries, not guessed.

Pagination beyond the first page wasn't found (see config comment) —
this only fetches page 1, a small accepted gap.
"""

import hashlib
import html
import json
import logging
import re

from bs4 import BeautifulSoup

from tracker.config import company_enabled, load_yaml
from tracker.db import active_posting_ids_for_company, get_connection, mark_delisted, upsert_cards
from tracker.detail_parser import parse_detail_page
from tracker.fetch import PoliteSession
from tracker.listing_parser import Card

log = logging.getLogger(__name__)

SOURCE = "phenom"
_JOBS_MARKER = '"eagerLoadRefineSearch"'


def _slugify(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")


def _extract_jobs_payload(html_text: str) -> dict | None:
    idx = html_text.find(_JOBS_MARKER)
    if idx == -1:
        return None
    start = html_text.find("{", idx + len(_JOBS_MARKER))
    if start == -1:
        return None

    depth, in_str, esc, i = 0, False, False, start
    while i < len(html_text):
        c = html_text[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1

    try:
        return json.loads(html_text[start:i])
    except json.JSONDecodeError:
        return None


def fetch_company(session: PoliteSession, company: dict) -> list[Card]:
    response = session.get(company["search_url"])
    payload = _extract_jobs_payload(response.text)
    if payload is None:
        log.error("company=%s could not find embedded job data on search page", company["name"])
        return []

    jobs = payload.get("data", {}).get("jobs", [])
    total = payload.get("totalHits")
    if total is not None and total > len(jobs):
        log.info(
            "company=%s only page 1 available (%d of %d total) — see companies_phenom.yaml",
            company["name"],
            len(jobs),
            total,
        )

    cards: dict[int, Card] = {}
    for j in jobs:
        job_id_raw = j.get("jobId") or j.get("reqId") or ""
        digits = re.sub(r"\D", "", job_id_raw)
        if not digits:
            continue
        posting_id = int(digits)

        job_seq_no = j.get("jobSeqNo") or job_id_raw
        title = j.get("title", "")
        url = f"https://{company['host']}/global/en/job/{job_seq_no}/{_slugify(title)}"
        raw = json.dumps(j, sort_keys=True)

        cards[posting_id] = Card(
            posting_id=posting_id,
            url=url,
            title=title,
            company=company["name"],
            location=j.get("cityStateCountry") or j.get("location"),
            experience_level=j.get("experienceLevel"),
            language_req=None,
            benefits=None,
            tech_stack=None,
            task_summary=j.get("descriptionTeaser"),
            raw_card_html=raw,
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            work_arrangement=None,
        )
    return list(cards.values())


def fetch_detail(session: PoliteSession, url: str) -> dict | None:
    html_text = session.get(url).text
    detail = parse_detail_page(html_text)
    if detail is None:
        return None

    description_text = None
    if detail.description:
        unescaped = html.unescape(detail.description)
        description_text = BeautifulSoup(unescaped, "lxml").get_text(separator=" ", strip=True)

    return {
        "raw_detail_html": detail.raw_detail_html,
        "detail_text": description_text,
        "job_location_type": detail.job_location_type,
        "experience_level": None,
    }


def run_phenom_crawl() -> dict:
    config = load_yaml("companies_phenom.yaml")
    conn = get_connection()

    results: dict = {}
    all_seen_ids: set = set()
    try:
        for company in config["companies"]:
            if not company_enabled(company["name"]):
                preserved = active_posting_ids_for_company(conn, SOURCE, company["name"])
                all_seen_ids.update(preserved)
                results[company["name"]] = {"skipped": True, "preserved_active": len(preserved)}
                log.info("company=%s skipped (disabled via .env)", company["name"])
                continue

            session = PoliteSession(f"https://{company['host']}")
            cards = fetch_company(session, company)
            stats = upsert_cards(conn, cards, source=SOURCE)
            all_seen_ids.update(c.posting_id for c in cards)
            results[company["name"]] = {**stats, "fetched": len(cards)}
            log.info("company=%s fetched=%d %s", company["name"], len(cards), stats)

        results["total_seen"] = len(all_seen_ids)
        if all_seen_ids:
            delisted = mark_delisted(conn, SOURCE, all_seen_ids)
            log.info("delisted %d postings absent from this crawl", delisted)
            results["delisted"] = delisted
        else:
            log.error(
                "phenom crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_phenom_crawl())
