"""Algolia adapter — covers companies in config/companies_algolia.yaml
(MSCI).

Algolia search-only API keys are, by Algolia's own design, meant to be
public — safe to use directly rather than reverse-engineered. Found
embedded in an inline <script> on the career page (window.AG_ID/AG_KEY/
AG_INDEX for MSCI), not fetched from any endpoint robots.txt could
restrict; confirmed no robots.txt restrictions on the Algolia API host
itself either.

One HTTP call per company gets everything: a facetFilters query on the
location field already returns the full description/qualifications
inline — same shape as SuccessFactors' RSS and Ashby's board data, no
separate detail-fetch stage needed. detail_text is populated directly
from this one response via save_detail() right after upsert.

objectID is a hex string, not an integer — folded into a stable 63-bit
int (same trick as ashby.py's UUID folding) since postings.posting_id is
BIGINT.
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

SOURCE = "algolia"


def _posting_id_from_object_id(object_id: str) -> int:
    return int(object_id, 16) & 0x7FFFFFFFFFFFFFFF


def _strip_html(html_text: str | None) -> str | None:
    if not html_text:
        return None
    return BeautifulSoup(html_text, "lxml").get_text(separator=" ", strip=True)


def _search(session: PoliteSession, company: dict) -> list[dict]:
    url = f"https://{company['app_id']}-dsn.algolia.net/1/indexes/{company['index']}/query"
    facet_value = f"{company['location_facet']}:{company['location_value']}"
    body = {"params": f'query=&facetFilters=[["{facet_value}"]]&hitsPerPage=100'}
    response = session.post(
        url,
        json=body,
        headers={
            "X-Algolia-API-Key": company["api_key"],
            "X-Algolia-Application-Id": company["app_id"],
        },
    )
    return response.json().get("hits", [])


def fetch_company(session: PoliteSession, company: dict) -> list[tuple[Card, str, str | None]]:
    hits = _search(session, company)
    results = []
    for hit in hits:
        object_id = hit.get("objectID")
        if not object_id:
            continue

        raw = json.dumps(hit, sort_keys=True)
        description_text = _strip_html(hit.get("description"))
        qualifications_text = _strip_html(hit.get("qualifications"))
        full_text = " \n ".join(t for t in (description_text, qualifications_text) if t) or None

        card = Card(
            posting_id=_posting_id_from_object_id(object_id),
            url=f"https://{company['site_host']}{hit.get('jd_url', '')}",
            title=hit.get("title", ""),
            company=company["name"],
            location=hit.get("display_location") or hit.get("location"),
            experience_level=None,
            language_req=None,
            benefits=None,
            tech_stack=None,
            task_summary=full_text,
            raw_card_html=raw,
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            work_arrangement=(hit.get("role_category") or "").lower() or None,
        )
        results.append((card, raw, full_text))
    return results


def run_algolia_crawl() -> dict:
    config = load_yaml("companies_algolia.yaml")
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

            session = PoliteSession(f"https://{company['app_id']}-dsn.algolia.net")
            entries = fetch_company(session, company)
            cards = [c for c, _, _ in entries]
            stats = upsert_cards(conn, cards, source=SOURCE)

            for card, raw, full_text in entries:
                save_detail(
                    conn,
                    card.posting_id,
                    raw_detail_html=raw,
                    detail_text=full_text,
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
                "algolia crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_algolia_crawl())
