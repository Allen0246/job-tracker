"""iCIMS adapter — covers companies in config/companies_icims.yaml
(One Identity).

The human-facing search page is a wrapper; real content loads in a
same-origin iframe. Appending &in_iframe=1 to any iCIMS URL (list or
detail) fetches that iframe's target directly, server-rendered, no JS
needed — confirmed via the page's own noscript <iframe
src="...&in_iframe=1"> fallback. robots.txt only disallows referral/
login/candidate/reminder sub-paths, not /jobs/search or a job's own
detail page.

Card-level data already includes a truncated description preview
(iCIMS_JobCardItem's "description" div), richer than most thin-card
sources — but still treated as thin for filter.py's purposes, since the
truncation could easily cut off the stack/direction keywords that
actually matter before the full detail is fetched.
"""

import hashlib
import logging
import re

from bs4 import BeautifulSoup

from tracker.config import company_enabled, load_yaml
from tracker.db import active_posting_ids_for_company, get_connection, mark_delisted, upsert_cards
from tracker.fetch import PoliteSession
from tracker.listing_parser import Card

log = logging.getLogger(__name__)

SOURCE = "icims"
_JOB_ID_RE = re.compile(r"/jobs/(\d+)/")


def _iframe_url(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}in_iframe=1"


def _parse_jobs(soup: BeautifulSoup, company_name: str) -> list[Card]:
    cards: dict[int, Card] = {}
    for item in soup.select("li.iCIMS_JobCardItem"):
        a = item.select_one("div.title a")
        if a is None:
            continue
        href = a.get("href", "")
        m = _JOB_ID_RE.search(href)
        if not m:
            continue
        job_id = int(m.group(1))
        if job_id in cards:
            continue

        title_el = a.find("h3")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)

        loc_el = item.select_one("div.header.left span:not(.sr-only)")
        location = loc_el.get_text(strip=True) if loc_el else None

        desc_el = item.select_one("div.description")
        preview = desc_el.get_text(strip=True) if desc_el else None

        # Strip the in_iframe param back off for the stored URL — the
        # digest links to this, and a real browser should land on the
        # normal page, not the bare iframe target.
        clean_url = re.sub(r"[?&]in_iframe=1", "", href)
        raw = f"title={title}|location={location}|url={clean_url}"

        cards[job_id] = Card(
            posting_id=job_id,
            url=clean_url,
            title=title,
            company=company_name,
            location=location,
            experience_level=None,
            language_req=None,
            benefits=None,
            tech_stack=None,
            task_summary=preview,
            raw_card_html=raw,
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            work_arrangement=None,
        )
    return list(cards.values())


def fetch_company(session: PoliteSession, company: dict) -> list[Card]:
    response = session.get(_iframe_url(company["search_url"]))
    soup = BeautifulSoup(response.text, "lxml")
    return _parse_jobs(soup, company["name"])


def fetch_detail(session: PoliteSession, url: str) -> dict | None:
    response = session.get(_iframe_url(url))
    soup = BeautifulSoup(response.text, "lxml")
    container = soup.find(class_="iCIMS_Expandable_Text")
    if container is None:
        return None
    return {
        "raw_detail_html": response.text,
        "detail_text": container.get_text(separator=" ", strip=True),
        "job_location_type": None,
        "experience_level": None,
    }


def run_icims_crawl() -> dict:
    config = load_yaml("companies_icims.yaml")
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
                "icims crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_icims_crawl())
