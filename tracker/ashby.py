"""Ashby adapter — covers companies in config/companies_ashby.yaml
(SEON).

One HTTP call for the whole board: https://jobs.ashbyhq.com/<org> embeds
the full job list directly in a `window.__appData` JS blob — no request
to /api/ needed, which robots.txt disallows entirely on this host
(`Disallow: /api/`). Individual job detail pages embed the same
structure with the full HTML description already inline
(`posting.descriptionHtml`), so unlike Eightfold/Radancy there's no
separate thin-card-then-detail split needed — one request per posting
that matches gets everything, same shape as SuccessFactors' RSS.

Ashby job IDs are UUIDs, not integers — postings.posting_id is BIGINT,
so IDs are folded into a stable 63-bit int (negligible collision risk
at this project's scale) rather than stored verbatim.
"""

import hashlib
import json
import logging

from bs4 import BeautifulSoup

from tracker.config import company_enabled, load_yaml
from tracker.db import active_posting_ids_for_company, get_connection, mark_delisted, upsert_cards
from tracker.fetch import PoliteSession
from tracker.listing_parser import Card

log = logging.getLogger(__name__)

SOURCE = "ashby"
_BOARD_HOST = "jobs.ashbyhq.com"


def _posting_id_from_uuid(job_uuid: str) -> int:
    return int(job_uuid.replace("-", ""), 16) & 0x7FFFFFFFFFFFFFFF


def _extract_app_data(html: str) -> dict | None:
    idx = html.find("window.__appData")
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None

    depth, in_str, esc, i = 0, False, False, start
    while i < len(html):
        c = html[i]
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
        return json.loads(html[start:i])
    except json.JSONDecodeError:
        return None


def _matches_location(location: str | None, location_match: list[str]) -> bool:
    if not location:
        return False
    text = location.lower()
    return any(term in text for term in location_match)


def fetch_company(session: PoliteSession, company: dict, location_match: list[str]) -> list[Card]:
    response = session.get(f"https://{_BOARD_HOST}/{company['org_slug']}")
    app_data = _extract_app_data(response.text)
    if app_data is None:
        log.error("company=%s could not find window.__appData on board page", company["name"])
        return []

    postings = app_data.get("jobBoard", {}).get("jobPostings", [])
    cards = []
    for p in postings:
        location = p.get("locationName")
        if not _matches_location(location, location_match):
            continue

        job_uuid = p["id"]
        raw = json.dumps(p, sort_keys=True)

        cards.append(
            Card(
                posting_id=_posting_id_from_uuid(job_uuid),
                url=f"https://{_BOARD_HOST}/{company['org_slug']}/{job_uuid}",
                title=p.get("title", ""),
                company=company["name"],
                location=location,
                experience_level=None,
                language_req=None,
                benefits=None,
                tech_stack=None,
                task_summary=None,
                raw_card_html=raw,
                content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                work_arrangement=(p.get("workplaceType") or "").lower() or None,
            )
        )
    return cards


def fetch_detail(session: PoliteSession, url: str) -> dict | None:
    html = session.get(url).text
    app_data = _extract_app_data(html)
    if app_data is None or not app_data.get("posting"):
        return None

    posting = app_data["posting"]
    description_html = posting.get("descriptionHtml") or ""
    description_text = (
        BeautifulSoup(description_html, "lxml").get_text(separator=" ", strip=True)
        if description_html
        else None
    )
    return {
        "raw_detail_html": html,
        "detail_text": description_text,
        "job_location_type": None,
        "experience_level": None,
    }


def run_ashby_crawl() -> dict:
    config = load_yaml("companies_ashby.yaml")
    location_match = config["location_match"]
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

            session = PoliteSession(f"https://{_BOARD_HOST}")
            cards = fetch_company(session, company, location_match)
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
                "ashby crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_ashby_crawl())
