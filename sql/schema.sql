CREATE TABLE IF NOT EXISTS postings (
    posting_id        BIGINT PRIMARY KEY,
    source            TEXT NOT NULL DEFAULT 'profession_hu',
    url               TEXT NOT NULL,

    title             TEXT NOT NULL,
    company           TEXT,
    location          TEXT,
    salary_band       TEXT,
    work_arrangement  TEXT,              -- onsite / hybrid / remote, best-effort from crawl target + card/detail text
    experience_level  TEXT,              -- raw text, e.g. "1-3 years experience"
    language_req      TEXT,
    benefits          TEXT,
    tech_stack        TEXT,              -- card's own "Tech stack" bullet list, when present
    task_summary      TEXT,

    raw_card_html     TEXT NOT NULL,
    raw_detail_html   TEXT,
    detail_text       TEXT,              -- JSON-LD JobPosting.description: tasks+requirements+benefits+company, concatenated
    detail_location_type TEXT,           -- JSON-LD JobPosting.jobLocationType, e.g. "TELECOMMUTE"
    detail_fetched_at TIMESTAMPTZ,

    content_hash      TEXT NOT NULL,     -- sha256 of raw_card_html; detects edits (e.g. salary added later)

    score             NUMERIC,           -- last-computed filter score (cache/audit only, not authoritative)
    gate_stack        BOOLEAN,
    gate_level        BOOLEAN,
    gate_direction    BOOLEAN,

    first_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
    delisted_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sent_log (
    posting_id  BIGINT NOT NULL REFERENCES postings(posting_id),
    digest_date DATE NOT NULL,
    PRIMARY KEY (posting_id, digest_date)
);

CREATE INDEX IF NOT EXISTS idx_postings_last_seen ON postings (last_seen);
CREATE INDEX IF NOT EXISTS idx_postings_delisted_at ON postings (delisted_at);
