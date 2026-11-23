import psycopg2
import psycopg2.extras

from tracker.config import env

_UPSERT_SQL = """
INSERT INTO postings (
    posting_id, source, url, title, company, location, work_arrangement,
    experience_level, language_req, benefits, tech_stack, task_summary,
    raw_card_html, content_hash
) VALUES (
    %(posting_id)s, 'profession_hu', %(url)s, %(title)s, %(company)s, %(location)s, %(work_arrangement)s,
    %(experience_level)s, %(language_req)s, %(benefits)s, %(tech_stack)s, %(task_summary)s,
    %(raw_card_html)s, %(content_hash)s
)
ON CONFLICT (posting_id) DO UPDATE SET
    url = EXCLUDED.url,
    title = EXCLUDED.title,
    company = EXCLUDED.company,
    location = EXCLUDED.location,
    work_arrangement = EXCLUDED.work_arrangement,
    experience_level = EXCLUDED.experience_level,
    language_req = EXCLUDED.language_req,
    benefits = EXCLUDED.benefits,
    tech_stack = EXCLUDED.tech_stack,
    task_summary = EXCLUDED.task_summary,
    raw_card_html = EXCLUDED.raw_card_html,
    content_hash = EXCLUDED.content_hash,
    last_seen = now(),
    delisted_at = NULL
"""


def get_connection():
    return psycopg2.connect(env("DATABASE_URL", required=True))


def upsert_cards(conn, cards: list) -> dict:
    """Insert or refresh a batch of parsed listing cards.

    Returns counts of {inserted, updated_changed, updated_unchanged} for
    visibility into how much of a crawl was actually new information.
    """
    stats = {"inserted": 0, "updated_changed": 0, "updated_unchanged": 0}
    with conn.cursor() as cur:
        for card in cards:
            cur.execute(
                "SELECT content_hash FROM postings WHERE posting_id = %s",
                (card.posting_id,),
            )
            row = cur.fetchone()
            existing_hash = row[0] if row else None

            cur.execute(
                _UPSERT_SQL,
                {
                    "posting_id": card.posting_id,
                    "url": card.url,
                    "title": card.title,
                    "company": card.company,
                    "location": card.location,
                    "work_arrangement": card.work_arrangement,
                    "experience_level": card.experience_level,
                    "language_req": card.language_req,
                    "benefits": card.benefits,
                    "tech_stack": card.tech_stack,
                    "task_summary": card.task_summary,
                    "raw_card_html": card.raw_card_html,
                    "content_hash": card.content_hash,
                },
            )

            if existing_hash is None:
                stats["inserted"] += 1
            elif existing_hash != card.content_hash:
                stats["updated_changed"] += 1
            else:
                stats["updated_unchanged"] += 1

    conn.commit()
    return stats


def fetch_active_postings(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT posting_id, url, title, tech_stack, task_summary, benefits,
                   language_req, experience_level, detail_text, detail_fetched_at
            FROM postings
            WHERE delisted_at IS NULL
            """
        )
        return [dict(row) for row in cur.fetchall()]


def save_score(conn, posting_id: int, result: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE postings
            SET score = %(score)s, gate_stack = %(gate_stack)s,
                gate_level = %(gate_level)s, gate_direction = %(gate_direction)s
            WHERE posting_id = %(posting_id)s
            """,
            {**result, "posting_id": posting_id},
        )
    conn.commit()


def save_detail(conn, posting_id: int, detail) -> None:
    work_arrangement_override = "remote" if detail.job_location_type == "TELECOMMUTE" else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE postings
            SET raw_detail_html = %(raw_detail_html)s,
                detail_text = %(detail_text)s,
                detail_location_type = %(detail_location_type)s,
                detail_fetched_at = now(),
                work_arrangement = COALESCE(%(work_arrangement_override)s, work_arrangement)
            WHERE posting_id = %(posting_id)s
            """,
            {
                "raw_detail_html": detail.raw_detail_html,
                "detail_text": detail.description,
                "detail_location_type": detail.job_location_type,
                "work_arrangement_override": work_arrangement_override,
                "posting_id": posting_id,
            },
        )
    conn.commit()


def mark_delisted(conn, source: str, seen_ids: set) -> int:
    """Marks postings absent from the latest crawl as delisted.

    Scoped to `source` and requires a non-empty seen_ids: a crawl that
    silently returned nothing (site outage, blocked request, parser
    break) must not be allowed to wipe out every previously-active
    posting. Callers should treat an empty seen_ids as a failed run and
    skip calling this rather than pass it through.
    """
    if not seen_ids:
        raise ValueError("refusing to mark delisted from an empty seen_ids set")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE postings
            SET delisted_at = now()
            WHERE delisted_at IS NULL AND source = %s AND NOT (posting_id = ANY(%s))
            """,
            (source, list(seen_ids)),
        )
        count = cur.rowcount
    conn.commit()
    return count


def unsent_survivors(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT p.posting_id, p.url, p.title, p.company, p.location,
                   p.work_arrangement, p.benefits, p.task_summary, p.score
            FROM postings p
            LEFT JOIN sent_log s ON s.posting_id = p.posting_id
            WHERE p.gate_stack AND p.gate_level AND p.gate_direction
              AND p.delisted_at IS NULL
              AND s.posting_id IS NULL
            ORDER BY p.score DESC
            """
        )
        return [dict(row) for row in cur.fetchall()]


def mark_sent(conn, posting_ids: list[int], digest_date) -> None:
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO sent_log (posting_id, digest_date) VALUES %s ON CONFLICT DO NOTHING",
            [(pid, digest_date) for pid in posting_ids],
        )
    conn.commit()
