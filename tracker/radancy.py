"""Radancy adapter — covers companies in config/companies_radancy.yaml
(Citi, BlackRock).

robots.txt disallows /search-jobs/ on both hosts — the location-filtered
search UI is off-limits, confirmed via a real DevTools capture: the
facet-filter AJAX request that applies a location filter is
`/search-jobs/results?...`, itself under the disallowed path. There is
no legitimate way to get server-side location filtering here. The
compliant alternative: sitemap.xml lists
/category/<slug>/<org_id>/<category_id>/<page> pages, not covered by
that rule, confirmed allowed — but these return global results, so
location filtering happens client-side, after fetch.

List markup is NOT uniform across companies despite being the same
vendor (confirmed the hard way — the first version of this parser only
matched BlackRock's shape and silently found 0 for Citi): Citi uses
`sr-job-item__link`/`sr-job-item__title`/`sr-job-location`, BlackRock
uses `section3__search-results-a`/`section3__job-title`/`job-location`.
Both put `data-job-id` directly on the job-title anchor though, so
parsing keys off that instead of a class name, with a scoped search
for title/location that tolerates either shape.

Categories can be large (confirmed: Citi's technology-jobs alone has
~990 postings globally) with no way to filter before paginating, so
per-category pagination is capped rather than exhaustive — trades
completeness for reasonable request volume on an already-heavier vendor.

Two crawl modes, chosen automatically:
  full scan   — paginate every category up to _MAX_PAGES, same as
                before. Runs on the first-ever crawl and at least once
                every _FULL_SCAN_INTERVAL_DAYS after that. Only a full
                scan is trustworthy enough to drive delisting, since
                delisting assumes "we saw the whole listing."
  early stop  — paginate only until a page's job IDs are all at or
                below the watermark recorded by the last full scan
                (job IDs here are roughly time-ordered, confirmed
                empirically), for _EARLY_STOP_PAGES pages in a row.
                Much cheaper on the far more common case of "nothing
                new happened since yesterday." Never touches delisting
                — it hasn't seen enough of the listing to trust that a
                missing posting is actually gone rather than just past
                where it stopped looking.
"""

import datetime
import hashlib
import logging
import re

from bs4 import BeautifulSoup

from tracker.config import company_enabled, load_yaml
from tracker.db import (
    active_posting_ids_for_company,
    get_connection,
    get_watermark,
    last_full_scan_at,
    mark_delisted,
    set_watermark,
    upsert_cards,
)
from tracker.fetch import PoliteSession
from tracker.listing_parser import Card

log = logging.getLogger(__name__)

SOURCE = "radancy"
_MAX_PAGES = 15  # cap, not a true "end of results" bound — see module docstring
_FULL_SCAN_INTERVAL_DAYS = 7
_EARLY_STOP_PAGES = 2  # consecutive all-below-watermark pages before stopping early

_LOCATION_CLASS_RE = re.compile(r"location", re.I)
_TITLE_CLASS_RE = re.compile(r"job-title", re.I)


def _category_url(company: dict, category: dict, page: int) -> str:
    base = f"https://{company['host']}/category/{category['slug']}/{company['org_id']}/{category['id']}/1"
    return base if page == 1 else f"{base}/{page}"


def _matches_location(location: str | None, location_match: list[str]) -> bool:
    if not location:
        return False
    text = location.lower()
    return any(term in text for term in location_match)


def _find_scope(a):
    # Citi: location is a sibling of the anchor's <h3> wrapper, not a
    # descendant of the anchor itself — widen the search to the
    # enclosing job-item. BlackRock: location IS inside the anchor, so
    # this parent lookup just fails and falls back to `a` itself.
    return a.find_parent(class_="sr-job-item") or a


def _extract_title(a, scope) -> str:
    title_el = scope.find(["h2", "h3"], class_=_TITLE_CLASS_RE)
    return title_el.get_text(strip=True) if title_el else a.get_text(strip=True)


def _extract_location(scope) -> str | None:
    loc_el = scope.find(class_=_LOCATION_CLASS_RE)
    if loc_el is None:
        return None
    # BlackRock nests the actual value in a child span; Citi's element
    # IS the value directly.
    value_el = loc_el.find(class_="section3__job-info")
    text = (value_el or loc_el).get_text(strip=True)
    text = re.sub(r"^location:?\s*", "", text, flags=re.I).strip()
    return text or None


def _parse_jobs(soup: BeautifulSoup, host: str, company_name: str, location_match: list[str]) -> list[Card]:
    cards: dict[int, Card] = {}
    for a in soup.find_all("a", attrs={"data-job-id": True}):
        job_id = a.get("data-job-id")
        if not job_id or int(job_id) in cards:
            continue

        scope = _find_scope(a)
        title = _extract_title(a, scope)
        location = _extract_location(scope)

        if not _matches_location(location, location_match):
            continue

        href = a.get("href", "")
        url = f"https://{host}{href}" if href.startswith("/") else href
        raw = f"title={title}|location={location}|url={url}"

        cards[int(job_id)] = Card(
            posting_id=int(job_id),
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
    return list(cards.values())


def fetch_category(
    session: PoliteSession,
    company: dict,
    category: dict,
    location_match: list[str],
    watermark: int | None,
    full_scan: bool,
) -> tuple[list[Card], int]:
    all_cards: dict[int, Card] = {}
    max_id_seen = watermark or 0
    consecutive_below_watermark = 0

    for page in range(1, _MAX_PAGES + 1):
        url = _category_url(company, category, page)
        response = session.get(url)
        soup = BeautifulSoup(response.text, "lxml")

        page_ids = [int(a["data-job-id"]) for a in soup.find_all("a", attrs={"data-job-id": True}) if a.get("data-job-id")]
        if not page_ids:
            break
        max_id_seen = max(max_id_seen, *page_ids)

        for card in _parse_jobs(soup, company["host"], company["name"], location_match):
            all_cards[card.posting_id] = card

        if not full_scan and watermark is not None:
            if all(pid <= watermark for pid in page_ids):
                consecutive_below_watermark += 1
                if consecutive_below_watermark >= _EARLY_STOP_PAGES:
                    break
            else:
                consecutive_below_watermark = 0
    else:
        if full_scan:
            log.warning(
                "company=%s category=%s hit the %d-page cap — coverage may be "
                "incomplete for large categories",
                company["name"],
                category["slug"],
                _MAX_PAGES,
            )

    return list(all_cards.values()), max_id_seen


def fetch_detail(session: PoliteSession, url: str) -> dict | None:
    html = session.get(url).text
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(class_="job-description")
    if container is None:
        return None
    return {
        "raw_detail_html": html,
        "detail_text": container.get_text(separator=" ", strip=True),
        "job_location_type": None,
        "experience_level": None,
    }


def _should_full_scan(conn) -> bool:
    last = last_full_scan_at(conn, SOURCE)
    if last is None:
        return True
    age = datetime.datetime.now(datetime.timezone.utc) - last
    return age >= datetime.timedelta(days=_FULL_SCAN_INTERVAL_DAYS)


def run_radancy_crawl() -> dict:
    config = load_yaml("companies_radancy.yaml")
    location_match = config["location_match"]
    conn = get_connection()

    results: dict = {}
    all_seen_ids: set = set()
    try:
        full_scan = _should_full_scan(conn)
        results["full_scan"] = full_scan
        log.info("mode=%s", "full_scan" if full_scan else "early_stop")

        for company in config["companies"]:
            if not company_enabled(company["name"]):
                preserved = active_posting_ids_for_company(conn, SOURCE, company["name"])
                all_seen_ids.update(preserved)
                results[company["name"]] = {"skipped": True, "preserved_active": len(preserved)}
                log.info("company=%s skipped (disabled via .env)", company["name"])
                continue

            session = PoliteSession(f"https://{company['host']}")
            company_cards: dict[int, Card] = {}
            for category in company["categories"]:
                scope = f"{company['name']}:{category['slug']}"
                watermark = get_watermark(conn, SOURCE, scope)
                cards, max_id_seen = fetch_category(
                    session, company, category, location_match, watermark, full_scan
                )
                set_watermark(conn, SOURCE, scope, max_id_seen)
                for c in cards:
                    company_cards[c.posting_id] = c
                log.info(
                    "company=%s category=%s matched=%d watermark=%s->%d",
                    company["name"],
                    category["slug"],
                    len(cards),
                    watermark,
                    max_id_seen,
                )

            cards = list(company_cards.values())
            stats = upsert_cards(conn, cards, source=SOURCE)
            all_seen_ids.update(c.posting_id for c in cards)
            results[company["name"]] = {**stats, "fetched": len(cards)}
            log.info("company=%s fetched=%d %s", company["name"], len(cards), stats)

        results["total_seen"] = len(all_seen_ids)
        if not full_scan:
            # Early-stop runs don't see enough of the listing to trust
            # that a posting's absence means it's actually gone, rather
            # than just past where pagination stopped.
            log.info("early-stop run — skipping delisting until the next full scan")
            results["delisted"] = None
        elif all_seen_ids:
            delisted = mark_delisted(conn, SOURCE, all_seen_ids)
            log.info("delisted %d postings absent from this crawl", delisted)
            results["delisted"] = delisted
        else:
            log.error(
                "radancy crawl saw zero postings across all companies — "
                "skipping delisting to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_radancy_crawl())
