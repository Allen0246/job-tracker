"""Selects survivors never previously sent and emails them.

sent_log dedup is permanent, not per-day: a posting sent once is never
sent again, even if it's still active and still passes the filter on a
later run. This is what lets keyword tuning be a safe re-run over
months of stored history instead of re-flooding the inbox with
everything that newly qualifies.
"""

import datetime
import logging
from collections import defaultdict
from html import escape

from tracker.db import get_connection, mark_sent, unsent_survivors
from tracker.mailer import send_email

log = logging.getLogger(__name__)


def _group_key(p: dict) -> str:
    # profession.hu is a broad aggregator spanning hundreds of small
    # companies — a section per company there is just noise. It stays
    # one umbrella group. Curated career-site sources (Eightfold, and
    # more as they're added) are a small, known set of target companies,
    # so the company name itself is the useful grouping — nobody reading
    # the digest cares that a posting came through "Eightfold", they
    # care that it's Morgan Stanley.
    if p["source"] == "profession_hu":
        return "profession.hu"
    return p["company"] or "Unknown"


def _render_html(postings: list[dict]) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in postings:
        groups[_group_key(p)].append(p)

    # unsent_survivors() returns postings pre-sorted by score DESC, so
    # each group's first posting is already its highest scorer — order
    # sections by that to keep the most relevant groups on top.
    ordered_groups = sorted(groups, key=lambda g: groups[g][0]["score"], reverse=True)

    sections = []
    for group in ordered_groups:
        rows = []
        for p in groups[group]:
            rows.append(
                f"""
                <tr>
                  <td style="padding:8px 12px;border-bottom:1px solid #ddd;">
                    <a href="{escape(p['url'])}"><strong>{escape(p['title'])}</strong></a><br>
                    {escape(p['company'] or '')} &mdash; {escape(p['location'] or '')}
                    ({escape(p['work_arrangement'] or 'n/a')})<br>
                    <span style="color:#666;font-size:0.9em;">
                      {escape((p['task_summary'] or '')[:200])}
                    </span>
                  </td>
                </tr>
                """
            )
        sections.append(
            f"""
            <h3 style="margin-top:24px;">{escape(group)} ({len(groups[group])})</h3>
            <table style="border-collapse:collapse;width:100%;">
              {''.join(rows)}
            </table>
            """
        )

    return f"""
    <html><body>
    <h2>{len(postings)} new matching postings</h2>
    {''.join(sections)}
    </body></html>
    """


def run_digest() -> dict:
    conn = get_connection()
    try:
        postings = unsent_survivors(conn)
        if not postings:
            log.info("no new survivors, skipping send")
            return {"sent": 0}

        send_email(f"job digest — {len(postings)} new postings", _render_html(postings))
        mark_sent(conn, [p["posting_id"] for p in postings], datetime.date.today())
        return {"sent": len(postings)}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_digest())
