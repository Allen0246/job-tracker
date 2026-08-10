"""Scores stored postings against config/keywords.yaml.

Reads fresh from disk on every run, so tuning the criteria is a re-run
over already-crawled postings, never a re-crawl and never a code change.
First pass scores against card-level text only (cheap, no network).
Postings that aren't outright disqualified on level get their detail page
fetched and are re-scored with the richer text before being counted as a
survivor — card-level experience_level is often blank or wrong, so an
ambiguous (not disqualified, not yet confirmed) level gate gets a second
look at the real description rather than an auto-reject. For sources with
rich card text (profession.hu) this is additionally gated on stack +
direction already passing, so the far more numerous obvious non-matches
skip the extra request; sources with no real card-level text at all
(Eightfold — see _THIN_CARD_SOURCES) skip that pre-check entirely, since
requiring it there means almost nothing would ever qualify for a detail
fetch in the first place.
"""

import logging
import re
from urllib.parse import urlparse

from tracker import ashby, avature, eightfold, icims, phenom, radancy, successfactors, workday
from tracker.config import load_yaml
from tracker.db import fetch_active_postings, get_connection, save_detail, save_score
from tracker.detail_parser import parse_detail_page
from tracker.fetch import PoliteSession

log = logging.getLogger(__name__)

_TEXT_FIELDS = (
    "title",
    "tech_stack",
    "task_summary",
    "benefits",
    "language_req",
    "experience_level",
    "detail_text",
)


def _combined_text(row: dict) -> str:
    return " \n ".join(row[f] for f in _TEXT_FIELDS if row.get(f))


# Plain substring matching was silently rejecting good postings: "lead"
# (a level disqualifier) matched inside "industry-leading", and
# "architect" matched inside "architects" — a colleague's role mentioned
# in the text, not the posting's own seniority. Confirmed against a real
# Ericsson posting ("ENM Junior C++ Developer") that should have passed
# and didn't. Word-boundary matching fixes both, while staying tolerant
# of terms that start/end in punctuation (e.g. "c++") where \b would
# never match — a boundary is only required on sides that are actually
# alphanumeric.
def _term_pattern(term: str) -> str:
    prefix = r"\b" if term[0].isalnum() else ""
    suffix = r"\b" if term[-1].isalnum() else ""
    return f"{prefix}{re.escape(term)}{suffix}"


def _contains_term(text: str, term: str) -> bool:
    return re.search(_term_pattern(term.lower()), text) is not None


def _score_keywords(text: str, spec: dict) -> float:
    score = 0.0
    for kw, weight in spec.get("positive", {}).items():
        if _contains_term(text, kw):
            score += weight
    for kw, weight in spec.get("negative", {}).items():
        if _contains_term(text, kw):
            score += weight  # weight is already negative in config
    return score


def _disqualified(text: str, spec: dict) -> bool:
    return any(_contains_term(text, dq) for dq in spec.get("disqualifiers", []))


# Catches "5 years", "5-10 years", "10+ years", "5 éves", "5 év" — takes the
# lower bound of a range as the stated minimum. A flat "5 years experience"
# on a posting titled "Junior ..." never matched the old literal-string
# disqualifiers (only "5-10 years"/"10+ years"/">10 years" were listed), so
# it slipped through despite exceeding max_years_experience.
_YEARS_RE = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?\+?\s*(?:years?|yrs?|éves?)")


def _stated_years(text: str) -> list[int]:
    return [int(n) for n in _YEARS_RE.findall(text)]


def _exceeds_experience_cap(text: str, spec: dict) -> bool:
    max_years = spec.get("max_years_experience")
    if max_years is None:
        return False
    return any(y > max_years for y in _stated_years(text))


def _level_disqualified(text: str, spec: dict, source: str | None = None) -> bool:
    # Numeric cap always wins regardless of source: a stated figure over
    # the cap is unambiguous evidence, title wording notwithstanding.
    if _exceeds_experience_cap(text, spec):
        return True

    word_disqualified = _disqualified(text, spec)
    # Outside profession.hu, an explicit low years figure overrides a
    # bare seniority word ("senior"/"lead"/...): some enterprise ATS
    # sites (confirmed on EPAM) title a role "Senior" as an internal
    # pay-grade label even when the body explicitly asks for 2-3 years —
    # unlike profession.hu, where "senior" in the text reliably tracks
    # the actual requirement. Requires an explicit number, not just the
    # absence of one — silence isn't evidence either way.
    if word_disqualified and source != "profession_hu" and _stated_years(text):
        return False
    return word_disqualified


def _level_gate(
    text: str, experience_level: str | None, spec: dict, source: str | None = None
) -> bool:
    if _level_disqualified(text, spec, source):
        return False
    ok_terms = spec.get("ok_experience_text", [])
    if any(_contains_term(text, ok) for ok in ok_terms):
        return True

    # A separate list matched ONLY against the structured experience_level
    # field, never the general title/description text: terms like "early
    # career" are precise controlled-vocabulary values on the one source
    # that sets them (GE HealthCare's Phenom field cleanly distinguishes
    # "Early Career" from "Co-op/Intern" and "Mid-Career"), but the same
    # phrase showing up as loose marketing copy in another source's body
    # text ("our early career program...") isn't the same reliable signal
    # — so it doesn't get to piggyback on the general text match above.
    exp_text = (experience_level or "").lower()
    field_only_terms = spec.get("ok_experience_level_values", [])
    return any(_contains_term(exp_text, v) for v in field_only_terms)


def score_posting(row: dict, keywords: dict) -> dict:
    text = _combined_text(row).lower()
    source = row.get("source")

    stack_score = _score_keywords(text, keywords["stack"])
    direction_score = _score_keywords(text, keywords["direction"])

    return {
        "score": stack_score + direction_score,
        "gate_stack": stack_score >= keywords["stack"]["threshold"],
        "gate_direction": (
            direction_score >= keywords["direction"]["threshold"]
            and not _disqualified(text, keywords["direction"])
        ),
        "gate_level": _level_gate(text, row.get("experience_level"), keywords["level"], source),
        "level_disqualified": _level_disqualified(text, keywords["level"], source),
    }


def _is_survivor(result: dict) -> bool:
    return result["gate_stack"] and result["gate_level"] and result["gate_direction"]


# Sources whose list response has no real free text to score against —
# no tech_stack, no task_summary, just a title (see eightfold.py). For
# these, requiring gate_stack+gate_direction to pass on card data first
# means they almost never trigger a detail fetch at all: a title alone
# essentially never scores 3+ on stack. Confirmed empirically — a real
# crawl of 142 Eightfold postings produced detail_fetched=0 under the
# stricter rule. Only a hard level disqualifier (from the title) should
# block the request for these; the real scoring happens in the detail.
_THIN_CARD_SOURCES = {"eightfold", "successfactors", "radancy", "ashby", "workday", "avature", "icims", "phenom"}


def _worth_detail_fetch(row: dict, result: dict) -> bool:
    """Card text is often missing or wrong on experience level, so a
    posting that isn't outright senior-disqualified still deserves a look
    at the real description before being rejected on thin card data.
    """
    if row.get("detail_fetched_at") or result["level_disqualified"]:
        return False
    if row.get("source") in _THIN_CARD_SOURCES:
        return True
    return result["gate_stack"] and result["gate_direction"]


def _fetch_detail_profession_hu(session: PoliteSession, row: dict) -> dict | None:
    html = session.get(row["url"]).text
    detail = parse_detail_page(html)
    if detail is None:
        return None
    return {
        "raw_detail_html": detail.raw_detail_html,
        "detail_text": detail.description,
        "job_location_type": detail.job_location_type,
        "experience_level": None,
    }


def _fetch_detail(
    row: dict, session: PoliteSession, eightfold_companies: dict, workday_companies: dict
) -> dict | None:
    source = row["source"]
    if source == "profession_hu":
        return _fetch_detail_profession_hu(session, row)
    if source == "eightfold":
        host = urlparse(row["url"]).netloc
        company = eightfold_companies.get(host)
        if company is None:
            log.error("no eightfold company config matches host=%s (posting_id=%s)", host, row["posting_id"])
            return None
        return eightfold.fetch_detail(session, company, row["posting_id"])
    if source == "successfactors":
        return successfactors.fetch_detail(session, row["url"])
    if source == "radancy":
        return radancy.fetch_detail(session, row["url"])
    if source == "ashby":
        return ashby.fetch_detail(session, row["url"])
    if source == "workday":
        host = urlparse(row["url"]).netloc
        company = workday_companies.get(host)
        if company is None:
            log.error("no workday company config matches host=%s (posting_id=%s)", host, row["posting_id"])
            return None
        return workday.fetch_detail(session, company, row["url"])
    if source == "avature":
        return avature.fetch_detail(session, row["url"])
    if source == "icims":
        return icims.fetch_detail(session, row["url"])
    if source == "phenom":
        return phenom.fetch_detail(session, row["url"])
    log.error("no detail fetcher registered for source=%s (posting_id=%s)", source, row["posting_id"])
    return None


def run_filter() -> dict:
    keywords = load_yaml("keywords.yaml")
    eightfold_companies = {c["host"]: c for c in load_yaml("companies_eightfold.yaml")["companies"]}
    workday_companies = {c["host"]: c for c in load_yaml("companies_workday.yaml")["companies"]}
    conn = get_connection()
    sessions: dict[str, PoliteSession] = {}
    stats = {"scored": 0, "survivors": 0, "detail_fetched": 0}

    try:
        rows = fetch_active_postings(conn)
        log.info("scoring %d active postings", len(rows))
        for i, row in enumerate(rows, start=1):
            result = score_posting(row, keywords)
            stats["scored"] += 1

            survivor = _is_survivor(result)
            if _worth_detail_fetch(row, result):
                host = urlparse(row["url"]).netloc
                if host not in sessions:
                    sessions[host] = PoliteSession(f"https://{host}")

                try:
                    detail = _fetch_detail(row, sessions[host], eightfold_companies, workday_companies)
                except Exception:
                    log.exception("detail fetch failed for posting_id=%s", row["posting_id"])
                    detail = None

                if detail is not None:
                    save_detail(conn, row["posting_id"], **detail)
                    stats["detail_fetched"] += 1
                    row["detail_text"] = detail["detail_text"]
                    if detail["experience_level"]:
                        row["experience_level"] = detail["experience_level"]
                    result = score_posting(row, keywords)
                    survivor = _is_survivor(result)
                    log.info(
                        "detail fetched posting_id=%s survivor=%s (%d/%d)",
                        row["posting_id"],
                        survivor,
                        i,
                        len(rows),
                    )

            save_score(conn, row["posting_id"], result)
            if survivor:
                stats["survivors"] += 1

            if i % 100 == 0:
                log.info("scored %d/%d", i, len(rows))
    finally:
        conn.close()

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_filter())
