"""Workday adapter — covers companies in config/companies_workday.yaml
(Cloudera).

One documented API shape, the same across every Workday tenant:
  POST https://<host>/wday/cxs/<tenant>/<site>/jobs
    body: {"appliedFacets": {"locationCountry": [<facet_id>]}, "limit":
    N, "offset": 0, "searchText": ""}
  GET https://<host>/wday/cxs/<tenant>/<site><externalPath>
    (externalPath comes straight from the list response)
Confirmed cold-callable with no auth/cookies, and the locationCountry
facet filters server-side — unlike Eightfold/Radancy/Ashby, no
client-side location filtering is needed here at all. Neither path
falls under this host's robots.txt disallow (`Disallow: /refreshFacet/`
only), confirmed on the live host.

posting_id comes from bulletFields[0] in the list response — a plain
numeric requisition ID, confirmed to match the detail response's own
jobReqId. No UUID-folding needed here, unlike Ashby.
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

SOURCE = "workday"
_PAGE_SIZE = 20


def _list_url(company: dict) -> str:
    return f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}/jobs"


def fetch_company(session: PoliteSession, company: dict) -> list[Card]:
    cards: dict[int, Card] = {}
    offset = 0

    while True:
        body = {
            "appliedFacets": {"locationCountry": [company["location_country_facet_id"]]},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        response = session.post(_list_url(company), json=body)
        data = response.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break

        for p in postings:
            bullet = p.get("bulletFields") or []
            if not bullet or not str(bullet[0]).isdigit():
                continue
            posting_id = int(bullet[0])
            raw = json.dumps(p, sort_keys=True)

            cards[posting_id] = Card(
                posting_id=posting_id,
                url=f"https://{company['host']}/{company['site']}{p['externalPath']}",
                title=p.get("title", ""),
                company=company["name"],
                location=p.get("locationsText"),
                experience_level=None,
                language_req=None,
                benefits=None,
                tech_stack=None,
                task_summary=None,
                raw_card_html=raw,
                content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                work_arrangement=(p.get("remoteType") or "").lower() or None,
            )

        offset += len(postings)
        if offset >= data.get("total", offset):
            break

    return list(cards.values())


def fetch_detail(session: PoliteSession, company: dict, url: str) -> dict | None:
    prefix = f"https://{company['host']}/{company['site']}"
    external_path = url[len(prefix):] if url.startswith(prefix) else url
    detail_url = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}{external_path}"

    response = session.get(detail_url)
    data = response.json().get("jobPostingInfo")
    if not data:
        return None

    description_html = data.get("jobDescription") or ""
    description_text = (
        BeautifulSoup(description_html, "lxml").get_text(separator=" ", strip=True)
        if description_html
        else None
    )
    return {
        "raw_detail_html": json.dumps(data, sort_keys=True),
        "detail_text": description_text,
        "job_location_type": None,
        "experience_level": None,
    }


def run_workday_crawl() -> dict:
    config = load_yaml("companies_workday.yaml")
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
                "workday crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_workday_crawl())
