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

-- One row per source per run_daily execution. Lets "a source normally
-- returns 40 and returns 0" be caught by querying history instead of
-- grepping logs — the failure mode that quietly looks like "nobody is
-- hiring" if nothing tracks what a source's healthy baseline looks like.
CREATE TABLE IF NOT EXISTS crawl_runs (
    id            SERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    fetched_count INTEGER,          -- distinct postings seen this run, NULL if the crawl errored before counting
    success       BOOLEAN NOT NULL,
    error_message TEXT,
    full_scan     BOOLEAN NOT NULL DEFAULT true  -- false = an early-stop run (see scan_watermarks); delisting is
                                                  -- only trustworthy on full_scan runs, which saw the whole listing
);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_source_run_at ON crawl_runs (source, run_at);

-- Lets a paginated source (currently Radancy) skip re-scanning postings
-- it already covered on a previous full scan: the highest job ID seen
-- per (source, scope) — scope is adapter-defined, e.g. "Citi:technology
-- -jobs" — stands in for "how far I've already looked," without needing
-- to have actually stored every ID that scrolled past (Radancy only
-- stores Budapest/Hungary matches, so most IDs seen are never in
-- `postings` at all). Job IDs here are assumed roughly time-ordered,
-- confirmed empirically for Radancy (a real page came back in strictly
-- descending numeric ID order).
CREATE TABLE IF NOT EXISTS scan_watermarks (
    source     TEXT NOT NULL,
    scope      TEXT NOT NULL,
    max_job_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, scope)
);
