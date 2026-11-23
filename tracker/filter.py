"""Scores stored postings against config/keywords.yaml.

Reads fresh from disk on every run, so tuning the criteria is a re-run
over already-crawled postings, never a re-crawl and never a code change.
First pass scores against card-level text only (cheap, no network).
Postings that pass stack + direction and aren't outright disqualified on
level get their detail page fetched and are re-scored with the richer
text before being counted as a survivor — card-level experience_level is
often blank or wrong, so an ambiguous (not disqualified, not yet
confirmed) level gate gets a second look at the real description rather
than an auto-reject.
"""

import logging
import re

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
# in the text, not the posting's own seniority. Word-boundary matching
# fixes both, while staying tolerant of terms that start/end in
# punctuation (e.g. "c++") where \b would never match — a boundary is
# only required on sides that are actually alphanumeric.
#
# A trailing "*" in the config ("microservice*", "vezető*") drops the
# end boundary, so the term also matches with any suffix. Needed for
# plurals and above all for Hungarian, which inflects by suffix
# ("automatizációs", "minőségbiztosítási", "vezetőt") — a strict whole
# word would miss those.
#
# A leading "*" likewise drops the start boundary, for Hungarian
# compounds written as one word ("*menedzser" also hits
# "projektmenedzser", "*tesztelő" hits "szoftvertesztelő").
def _term_pattern(term: str) -> str:
    open_start, open_end = term.startswith("*"), term.endswith("*")
    term = term.strip("*")
    prefix = r"\b" if term[0].isalnum() and not open_start else ""
    suffix = r"\b" if term[-1].isalnum() and not open_end else ""
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


def _disqualified(text: str, title: str, spec: dict) -> bool:
    """`disqualifiers` are checked against the whole posting text,
    `title_disqualifiers` against the title only. The title-only list is
    for terms that identify the role itself ("QA Engineer", "Internship")
    but appear innocently in a normal developer description too ("work
    closely with our QA engineer", "we also run an internship program").
    """
    return any(_contains_term(text, dq) for dq in spec.get("disqualifiers", [])) or any(
        _contains_term(title, dq) for dq in spec.get("title_disqualifiers", [])
    )


# Catches "5 years", "5-10 years", "10+ years", "5 éves", "5 év" — takes the
# lower bound of a range as the stated minimum. A flat "5 years experience"
# on a posting titled "Junior ..." never matched the old literal-string
# disqualifiers (only "5-10 years"/"10+ years"/">10 years" were listed), so
# it slipped through despite exceeding max_years_experience.
_YEARS_RE = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?\+?\s*(?:years?|yrs?|éves?)")


def _exceeds_experience_cap(text: str, spec: dict) -> bool:
    max_years = spec.get("max_years_experience")
    if max_years is None:
        return False
    return any(int(n) > max_years for n in _YEARS_RE.findall(text))


def _level_disqualified(text: str, title: str, spec: dict) -> bool:
    return _disqualified(text, title, spec) or _exceeds_experience_cap(text, spec)


def _level_gate(text: str, title: str, experience_level: str | None, spec: dict) -> bool:
    if _level_disqualified(text, title, spec):
        return False
    exp_text = (experience_level or "").lower()
    ok_terms = spec.get("ok_experience_text", [])
    return any(_contains_term(exp_text, ok) or _contains_term(text, ok) for ok in ok_terms)


def score_posting(row: dict, keywords: dict) -> dict:
    text = _combined_text(row).lower()
    title = (row.get("title") or "").lower()

    stack_score = _score_keywords(text, keywords["stack"])
    direction_score = _score_keywords(text, keywords["direction"])

    return {
        "score": stack_score + direction_score,
        "gate_stack": stack_score >= keywords["stack"]["threshold"],
        "gate_direction": (
            direction_score >= keywords["direction"]["threshold"]
            and not _disqualified(text, title, keywords["direction"])
        ),
        "gate_level": _level_gate(text, title, row.get("experience_level"), keywords["level"]),
        "level_disqualified": _level_disqualified(text, title, keywords["level"]),
    }


def _is_survivor(result: dict) -> bool:
    return result["gate_stack"] and result["gate_level"] and result["gate_direction"]


def _worth_detail_fetch(row: dict, result: dict) -> bool:
    """Card text is often missing or wrong on experience level, so a
    posting that isn't outright senior-disqualified still deserves a look
    at the real description before being rejected on thin card data.
    """
    return (
        result["gate_stack"]
        and result["gate_direction"]
        and not result["level_disqualified"]
        and not row.get("detail_fetched_at")
    )


def run_filter() -> dict:
    keywords = load_yaml("keywords.yaml")
    conn = get_connection()
    session = None
    stats = {"scored": 0, "survivors": 0, "detail_fetched": 0}

    try:
        rows = fetch_active_postings(conn)
        log.info("scoring %d active postings", len(rows))
        for i, row in enumerate(rows, start=1):
            result = score_posting(row, keywords)
            stats["scored"] += 1

            survivor = _is_survivor(result)
            if _worth_detail_fetch(row, result):
                if session is None:
                    session = PoliteSession()
                try:
                    detail_html = session.get(row["url"]).text
                except Exception:
                    log.exception("detail fetch failed for posting_id=%s", row["posting_id"])
                else:
                    detail = parse_detail_page(detail_html)
                    if detail is not None:
                        save_detail(conn, row["posting_id"], detail)
                        stats["detail_fetched"] += 1
                        row["detail_text"] = detail.description
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
