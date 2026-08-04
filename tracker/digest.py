"""Selects survivors never previously sent and emails them.

sent_log dedup is permanent, not per-day: a posting sent once is never
sent again, even if it's still active and still passes the filter on a
later run. This is what lets keyword tuning be a safe re-run over
months of stored history instead of re-flooding the inbox with
everything that newly qualifies.
"""

import datetime
import logging
from html import escape

from tracker.db import get_connection, mark_sent, unsent_survivors
from tracker.mailer import send_email

log = logging.getLogger(__name__)


def _render_html(postings: list[dict]) -> str:
    rows = []
    for p in postings:
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
    return f"""
    <html><body>
    <h2>{len(postings)} new matching postings</h2>
    <table style="border-collapse:collapse;width:100%;">
      {''.join(rows)}
    </table>
    </body></html>
    """


def run_digest() -> dict:
    conn = get_connection()
    try:
        postings = unsent_survivors(conn)
        if not postings:
            log.info("no new survivors, skipping send")
            return {"sent": 0}

        send_email(f"profession.hu digest — {len(postings)} new postings", _render_html(postings))
        mark_sent(conn, [p["posting_id"] for p in postings], datetime.date.today())
        return {"sent": len(postings)}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_digest())
