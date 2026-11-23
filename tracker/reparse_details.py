"""Re-parse every stored detail page with the current detail_parser.

The raw detail HTML is kept in Postgres exactly so that a parser fix can
be applied to history without fetching anything again. Run this after
changing detail_parser.py; the next filter run then scores against the
fixed text:

    python -m tracker.reparse_details
"""

import logging

from tracker.db import get_connection
from tracker.detail_parser import parse_detail_page

log = logging.getLogger(__name__)


def run_reparse() -> dict:
    conn = get_connection()
    stats = {"reparsed": 0, "unparseable": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT posting_id FROM postings WHERE raw_detail_html IS NOT NULL")
            ids = [row[0] for row in cur.fetchall()]

        for posting_id in ids:
            with conn.cursor() as cur:
                cur.execute("SELECT raw_detail_html FROM postings WHERE posting_id = %s", (posting_id,))
                detail = parse_detail_page(cur.fetchone()[0])
                if detail is None:
                    stats["unparseable"] += 1
                    continue
                cur.execute(
                    "UPDATE postings SET detail_text = %s WHERE posting_id = %s",
                    (detail.description, posting_id),
                )
            stats["reparsed"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_reparse())
