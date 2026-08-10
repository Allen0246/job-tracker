"""Avature adapter — covers companies in config/companies_avature.yaml
(Siemens, broad search across all Siemens brands/subsidiaries).

Server-rendered list and detail pages, no API needed. Pagination is a
plain `&folderOffset=N` query param, N in steps of `records_per_page`
(the site's own `folderRecordsPerPage` value), confirmed against a real
"next page" link. robots.txt explicitly allows /externaljobs (only a
`qtvc=` tracking param is disallowed, which this adapter never sends).

posting_id comes from the trailing numeric segment of each job's
JobDetail URL.

The list page only gives a config-level placeholder company name — the
real per-posting legal entity (evosoft vs. Siemens Mobility Kft. vs.
Siemens Healthcare Kft. vs. ...) only appears in a "Company" field on
the detail page, so fetch_detail() also returns a `company` override
that filter.py's save_detail() call applies once the detail is fetched.
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

SOURCE = "avature"
_JOB_ID_RE = re.compile(r"/(\d+)/?$")
_MAX_PAGES = 100  # safety bound; real sites here have single-digit page counts


def _parse_jobs(soup: BeautifulSoup, company_name: str) -> list[Card]:
    cards: dict[int, Card] = {}
    for a in soup.select("h3.article__header__text__title a.link"):
        href = a.get("href", "")
        m = _JOB_ID_RE.search(href)
        if not m:
            continue
        job_id = int(m.group(1))
        if job_id in cards:
            continue

        title = a.get_text(strip=True)
        article = a.find_parent("article")
        city = article.find(class_="list-item-jobCity") if article else None
        state = article.find(class_="list-item-jobState") if article else None
        country = article.find(class_="list-item-jobCountry") if article else None
        location = (
            ", ".join(el.get_text(strip=True) for el in (city, state, country) if el is not None)
            or None
        )

        raw = f"title={title}|location={location}|url={href}"
        cards[job_id] = Card(
            posting_id=job_id,
            url=href,
            title=title,
            company=company_name,
            location=location,
            experience_level=None,
            language_req=None,
            benefits=None,
            tech_stack=None,
            task_summary=None,
            raw_card_html=raw,
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            work_arrangement=None,
        )
    return list(cards.values())


def fetch_company(session: PoliteSession, company: dict) -> list[Card]:
    all_cards: dict[int, Card] = {}
    offset = 0
    for _ in range(_MAX_PAGES):
        url = (
            f"{company['search_url']}&folderRecordsPerPage={company['records_per_page']}"
            f"&folderOffset={offset}"
        )
        response = session.get(url)
        soup = BeautifulSoup(response.text, "lxml")
        cards = _parse_jobs(soup, company["name"])
        if not cards:
            break
        for c in cards:
            all_cards[c.posting_id] = c
        offset += company["records_per_page"]
    return list(all_cards.values())


def _extract_company(soup: BeautifulSoup) -> str | None:
    for field in soup.select(".article__content__view__field"):
        label = field.select_one(".article__content__view__field__label")
        if label and label.get_text(strip=True) == "Company":
            value = field.select_one(".article__content__view__field__value")
            if value:
                return value.get_text(strip=True)
    return None


def fetch_detail(session: PoliteSession, url: str) -> dict | None:
    html = session.get(url).text
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(id="section0__content") or soup.find(class_="article__content")
    if container is None:
        return None
    return {
        "raw_detail_html": html,
        "detail_text": container.get_text(separator=" ", strip=True),
        "job_location_type": None,
        "experience_level": None,
        "company": _extract_company(soup),
    }


def run_avature_crawl() -> dict:
    config = load_yaml("companies_avature.yaml")
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
                "avature crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_avature_crawl())
