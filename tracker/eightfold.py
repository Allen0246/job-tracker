"""Eightfold ATS adapter — covers every Eightfold-hosted company in
config/companies_eightfold.yaml (Morgan Stanley, Ericsson, Vodafone).

One HTTP call is the public search UI's own API: GET
https://<host>/api/pcsx/search?domain=...&location=...&start=... returns
clean JSON directly, no scraping needed, confirmed cold-callable with a
bare User-Agent and explicitly allowed by robots.txt.

Unlike profession.hu's cards, Eightfold list items carry almost no free
text — no tech stack, no description, no benefits, just title/location/
department/dates. Real content only exists behind position_details, so
filter.py's detail-fetch pass carries nearly all the scoring weight here;
card-level fields mainly exist to let stack/direction reject the obvious
non-matches (title alone) before spending that extra request.
"""

import hashlib
import json
import logging
from urllib.parse import quote

from bs4 import BeautifulSoup

from tracker.config import company_enabled, load_yaml
from tracker.db import active_posting_ids_for_company, get_connection, mark_delisted, upsert_cards
from tracker.fetch import PoliteSession
from tracker.listing_parser import Card

log = logging.getLogger(__name__)

SOURCE = "eightfold"


def _search_url(company: dict, config: dict, start: int) -> str:
    return (
        f"https://{company['host']}/api/pcsx/search"
        f"?domain={company['domain']}&query=&location={quote(company['location'])}"
        f"&start={start}&sort_by=timestamp"
        f"&filter_distance={config['filter_distance']}"
        f"&filter_include_remote={config['filter_include_remote']}"
    )


def _to_card(item: dict, company: dict) -> Card:
    raw = json.dumps(item, sort_keys=True)
    locations = item.get("locations") or item.get("standardizedLocations") or []
    return Card(
        posting_id=item["id"],
        url=f"https://{company['host']}/careers/job/{item['id']}",
        title=item.get("name", ""),
        company=company["name"],
        location=locations[0] if locations else None,
        experience_level=None,  # only known after detail fetch, and only on some tenants
        language_req=None,  # not exposed by this API
        benefits=None,  # not exposed at list level
        tech_stack=None,  # not exposed at list level
        task_summary=None,  # not exposed at list level
        # Repurposed for a JSON source: the raw list-item JSON, not HTML.
        # Keeps the same "raw text survives a parser break" guarantee
        # without a schema migration for a column name that no longer
        # quite fits.
        raw_card_html=raw,
        content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        work_arrangement=item.get("workLocationOption"),
    )


def fetch_company(session: PoliteSession, company: dict, config: dict) -> list[Card]:
    cards: list[Card] = []
    start = 0
    while True:
        url = _search_url(company, config, start)
        response = session.get(url)
        data = response.json()["data"]
        positions = data["positions"]
        if not positions:
            break

        cards.extend(_to_card(p, company) for p in positions)
        start += len(positions)
        if start >= data["count"]:
            break

    return cards


def fetch_detail(session: PoliteSession, company: dict, posting_id: int) -> dict | None:
    """Fetches the real job description for one posting.

    List items carry almost no free text (see module docstring), so this
    is where the actual stack/direction/level signal comes from for this
    source. jobDescription is HTML; stripped to text same as any other
    detail_text. efcustomTextPcsPostingJobLevel is a structured level
    signal, but only Morgan Stanley's tenant exposes it — Ericsson and
    Vodafone detail records have no equivalent field at all, so for those
    two the free-text level gate (title/description) is the only signal.
    """
    url = (
        f"https://{company['host']}/api/pcsx/position_details"
        f"?position_id={posting_id}&domain={company['domain']}"
    )
    response = session.get(url)
    data = response.json().get("data")
    if not data:
        return None

    raw = json.dumps(data, sort_keys=True)
    description_html = data.get("jobDescription") or ""
    description_text = (
        BeautifulSoup(description_html, "lxml").get_text(separator=" ", strip=True)
        if description_html
        else None
    )
    level_tags = data.get("efcustomTextPcsPostingJobLevel")

    return {
        "raw_detail_html": raw,
        "detail_text": description_text,
        "job_location_type": None,  # work arrangement already known from list-level workLocationOption
        "experience_level": level_tags[0] if level_tags else None,
    }


def run_eightfold_crawl() -> dict:
    config = load_yaml("companies_eightfold.yaml")
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
            cards = fetch_company(session, company, config)
            stats = upsert_cards(conn, cards, source=SOURCE)
            all_seen_ids.update(card.posting_id for card in cards)
            results[company["name"]] = {**stats, "fetched": len(cards)}
            log.info("company=%s fetched=%d %s", company["name"], len(cards), stats)

        results["total_seen"] = len(all_seen_ids)
        if all_seen_ids:
            delisted = mark_delisted(conn, SOURCE, all_seen_ids)
            log.info("delisted %d postings absent from this crawl", delisted)
            results["delisted"] = delisted
        else:
            log.error(
                "eightfold crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_eightfold_crawl())
