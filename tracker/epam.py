"""EPAM adapter — covers companies in config/companies_epam.yaml
(EPAM).

Job data (including the full HTML description) is embedded directly in
the location-filtered page's own JS state, keyed
`"jobs":{"total":N,"jobs":[...]}`. Since the full description is
already there, this is a single-request source like Algolia/Ashby — no
separate detail-fetch stage, detail_text is populated directly at
crawl time.

robots.txt disallows /api and /api/* entirely, and query-string
filtering/pagination is separately disallowed (?page*, ?skill*,
?search*, ?query*). No compliant pagination mechanism was found, so
this only fetches what the bare location page renders server-side —
confirmed 10 of 106 Hungary postings, a known accepted gap.

posting_id comes from the `uid` field (Contentstack's "blt<hex>"
convention) — the "blt" prefix stripped, remaining hex folded into a
63-bit int, same trick as ashby.py's UUID folding.
"""

import hashlib
import json
import logging

from bs4 import BeautifulSoup

from tracker.config import company_enabled, load_yaml
from tracker.db import (
    active_posting_ids_for_company,
    get_connection,
    mark_delisted,
    save_detail,
    upsert_cards,
)
from tracker.fetch import PoliteSession
from tracker.listing_parser import Card

log = logging.getLogger(__name__)

SOURCE = "epam"
_JOBS_MARKER_PREFIX = '"jobs":{"total":'


def _posting_id_from_uid(uid: str) -> int:
    # Most postings use Contentstack's "blt<hex>" convention, but not
    # all (some come from a different backend pool — confirmed against
    # real data, not assumed) — hash generically so any string format
    # folds into a stable id rather than assuming hex.
    digest = hashlib.sha256(uid.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _extract_jobs_payload(html_text: str) -> dict | None:
    idx = html_text.find(_JOBS_MARKER_PREFIX)
    if idx == -1:
        return None
    start = idx + len('"jobs":')

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


def _extract_location(job: dict) -> str | None:
    city = job.get("city")
    if isinstance(city, list) and city:
        entry = city[0]
        name = entry.get("name")
        country_name = (entry.get("country") or {}).get("name")
        return f"{name}, {country_name}" if name and country_name else name

    country = job.get("country")
    if isinstance(country, list) and country:
        return country[0].get("name")
    return None


def fetch_company(session: PoliteSession, company: dict) -> list[tuple[Card, str, str | None]]:
    response = session.get(company["list_url"])
    payload = _extract_jobs_payload(response.text)
    if payload is None:
        log.error("company=%s could not find embedded job data on list page", company["name"])
        return []

    jobs = payload.get("jobs", [])
    total = payload.get("total")
    if total is not None and total > len(jobs):
        log.info(
            "company=%s only page 1 available (%d of %d total) — see companies_epam.yaml",
            company["name"],
            len(jobs),
            total,
        )

    results = []
    for j in jobs:
        uid = j.get("uid")
        seo_url = (j.get("seo") or {}).get("url")
        if not uid or not seo_url:
            continue

        raw = json.dumps(j, sort_keys=True)
        description_html = j.get("description") or ""
        description_text = (
            BeautifulSoup(description_html, "lxml").get_text(separator=" ", strip=True)
            if description_html
            else None
        )
        skills = j.get("skills") or []

        card = Card(
            posting_id=_posting_id_from_uid(uid),
            url=f"https://{company['host']}{seo_url}",
            title=j.get("name", ""),
            company=company["name"],
            location=_extract_location(j),
            experience_level=j.get("seniority"),
            language_req=None,
            benefits=None,
            tech_stack=", ".join(skills) if skills else None,
            task_summary=description_text,
            raw_card_html=raw,
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            work_arrangement=(j.get("vacancy_type") or "").lower() or None,
        )
        results.append((card, raw, description_text))
    return results


def run_epam_crawl() -> dict:
    config = load_yaml("companies_epam.yaml")
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
            entries = fetch_company(session, company)
            cards = [c for c, _, _ in entries]
            stats = upsert_cards(conn, cards, source=SOURCE)

            for card, raw, description_text in entries:
                save_detail(
                    conn,
                    card.posting_id,
                    raw_detail_html=raw,
                    detail_text=description_text,
                    job_location_type=None,
                    experience_level=None,
                )

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
                "epam crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_epam_crawl())
