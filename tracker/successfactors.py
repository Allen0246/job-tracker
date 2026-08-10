"""SuccessFactors (Career Site Builder) adapter — covers companies in
config/companies_successfactors.yaml (HCLTech, SAP, ONE Hungary).

Unlike Eightfold, this vendor has no single uniform page shape across
tenants — companies theme their Career Site Builder deployment heavily.
Two list-page shapes are confirmed (`method: static_page` / `tile_ajax`
in the config), but both render the actual job title link with the same
class name (`jobTitle-link`) and both detail pages use the same
description container (`class="jobdescription"`) — that stability is
what this parser leans on, not the surrounding per-company markup.

RSS (linked in every page's own <head>) was deliberately not used:
robots.txt disallows /services/ on all these hosts, and the feed lives
under /services/rss/category/ — confirmed blocked, not skipped by
assumption.

List pages carry no description text (title + location only), so like
Eightfold this is a thin-card source for filter.py's purposes — see
_THIN_CARD_SOURCES there.
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

SOURCE = "successfactors"

_JOB_ID_RE = re.compile(r"/(\d+)/?$")
_MAX_PAGES = 100  # safety bound; real sites here have single-digit page counts


def _find_location(soup: BeautifulSoup, job_id: int, anchor) -> str | None:
    # ONE Hungary style: per-job id, unique document-wide.
    el = soup.find(id=f"job-{job_id}-desktop-section-city-value")
    if el is not None:
        return el.get_text(strip=True)

    # SAP style: sibling span.jobLocation within the enclosing table row.
    row = anchor.find_parent("tr")
    if row is not None:
        loc = row.find(class_="jobLocation")
        if loc is not None:
            return loc.get_text(strip=True)

    return None


def _parse_jobs(html: str, host: str, company_name: str) -> list[Card]:
    soup = BeautifulSoup(html, "lxml")
    seen: dict[int, Card] = {}
    for a in soup.find_all("a", class_="jobTitle-link"):
        href = a.get("href", "")
        m = _JOB_ID_RE.search(href)
        if not m:
            continue
        job_id = int(m.group(1))
        if job_id in seen:
            continue

        title = a.get_text(strip=True)
        location = _find_location(soup, job_id, a)
        url = f"https://{host}{href}" if href.startswith("/") else href
        raw = f"title={title}|location={location}|url={url}"

        seen[job_id] = Card(
            posting_id=job_id,
            url=url,
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
    return list(seen.values())


def fetch_static_page(session: PoliteSession, company: dict) -> list[Card]:
    response = session.get(company["url"])
    return _parse_jobs(response.text, company["host"], company["name"])


def fetch_tile_ajax(session: PoliteSession, company: dict) -> list[Card]:
    all_cards: dict[int, Card] = {}

    response = session.get(company["url"])
    for card in _parse_jobs(response.text, company["host"], company["name"]):
        all_cards[card.posting_id] = card

    page_size = company["page_size"]
    startrow = page_size
    for _ in range(_MAX_PAGES):
        ajax_url = (
            f"https://{company['host']}{company['base_path']}"
            f"/tile-search-results/category/{company['catid']}?startrow={startrow}"
        )
        response = session.get(ajax_url)
        page_cards = _parse_jobs(response.text, company["host"], company["name"])
        if not page_cards:
            break

        new_count = sum(1 for c in page_cards if c.posting_id not in all_cards)
        for c in page_cards:
            all_cards.setdefault(c.posting_id, c)
        if new_count == 0:
            break  # pagination stopped advancing — avoid looping forever

        startrow += page_size

    return list(all_cards.values())


def fetch_detail(session: PoliteSession, url: str) -> dict | None:
    html = session.get(url).text
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(class_="jobdescription")
    if container is None:
        return None
    return {
        "raw_detail_html": html,
        "detail_text": container.get_text(separator=" ", strip=True),
        "job_location_type": None,
        "experience_level": None,
    }


def run_successfactors_crawl() -> dict:
    config = load_yaml("companies_successfactors.yaml")
    conn = get_connection()

    results = {}
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
            if company["method"] == "static_page":
                cards = fetch_static_page(session, company)
            elif company["method"] == "tile_ajax":
                cards = fetch_tile_ajax(session, company)
            else:
                raise ValueError(f"unknown method: {company['method']!r} for {company['name']}")

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
                "successfactors crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_successfactors_crawl())
