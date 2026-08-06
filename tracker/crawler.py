"""Walks the crawl targets defined in config/sources.yaml, paginating each
one and upserting parsed cards into Postgres.
"""

import logging

from tracker.config import load_yaml
from tracker.db import get_connection, mark_delisted, upsert_cards
from tracker.fetch import BASE_URL, PoliteSession
from tracker.listing_parser import parse_listing_page, result_count

log = logging.getLogger(__name__)

_RESULTS_PER_PAGE = 20  # observed; used only to size the initial page-count estimate


def _build_url(sources: dict, target: dict, page: int) -> str:
    category = sources["categories"][target["category"]]
    param_length = sources["param_length"]

    params = [0] * param_length
    params[0] = page
    params[1] = category["id"]

    slug_parts = [category["slug"]]

    if target.get("region"):
        region_key = target["region"]
        params[2] = sources["regions"][region_key]["id"]
        slug_parts.append(region_key.replace("_", "-"))

    if target.get("job_place_type"):
        job_place = sources["job_place_types"][target["job_place_type"]]
        params[36] = job_place["id"]

    slug = "/".join(slug_parts)
    return f"{BASE_URL}/allasok/{slug}/{','.join(str(p) for p in params)}"


def _infer_work_arrangement(location_text: str | None, target: dict) -> str:
    text = (location_text or "").lower()
    if "távmunka" in text or "remote" in text:
        return "remote"
    if "hibrid" in text:
        return "hybrid"
    if target.get("job_place_type") == "remote":
        return "remote"
    return "onsite"


def crawl_target(session: PoliteSession, conn, sources: dict, target: dict) -> tuple[dict, set]:
    totals = {"inserted": 0, "updated_changed": 0, "updated_unchanged": 0, "pages": 0}
    seen_ids: set = set()

    page = 1
    expected_total = None
    while True:
        url = _build_url(sources, target, page)
        response = session.get(url)
        html = response.text

        if expected_total is None:
            expected_total = result_count(html)
            log.info("target=%s expected_total=%s", target["name"], expected_total)

        cards = parse_listing_page(html)
        if not cards:
            break

        for card in cards:
            card.work_arrangement = _infer_work_arrangement(card.location, target)

        seen_ids.update(card.posting_id for card in cards)
        stats = upsert_cards(conn, cards)

        for key in ("inserted", "updated_changed", "updated_unchanged"):
            totals[key] += stats[key]
        totals["pages"] += 1

        log.info(
            "target=%s page=%d/%s new=%d changed=%d unchanged=%d",
            target["name"],
            page,
            (expected_total + _RESULTS_PER_PAGE - 1) // _RESULTS_PER_PAGE if expected_total else "?",
            totals["inserted"],
            totals["updated_changed"],
            totals["updated_unchanged"],
        )

        if expected_total is not None and page * _RESULTS_PER_PAGE >= expected_total:
            break
        page += 1

    return totals, seen_ids


def run_crawl() -> dict:
    sources = load_yaml("sources.yaml")
    session = PoliteSession()
    conn = get_connection()

    results = {}
    all_seen_ids: set = set()
    try:
        for target in sources["targets"]:
            totals, seen_ids = crawl_target(session, conn, sources, target)
            results[target["name"]] = totals
            all_seen_ids |= seen_ids

        if all_seen_ids:
            delisted = mark_delisted(conn, "profession_hu", all_seen_ids)
            log.info("delisted %d postings absent from this crawl", delisted)
            results["delisted"] = delisted
        else:
            log.error(
                "crawl saw zero postings across all targets — skipping delisting "
                "to avoid wiping every previously-active posting"
            )
            results["delisted"] = None
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_crawl())
