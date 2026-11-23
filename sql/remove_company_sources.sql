-- One-off cleanup for a database that was used while the company career-site
-- crawlers existed (see branch archive/company-crawlers). Removes their postings
-- so the profession.hu-only filter doesn't try to fetch and parse other sites'
-- pages, and drops the tables only those crawlers used. Safe to re-run.
BEGIN;

DELETE FROM sent_log
WHERE posting_id IN (SELECT posting_id FROM postings WHERE source <> 'profession_hu');

DELETE FROM postings WHERE source <> 'profession_hu';

DROP TABLE IF EXISTS crawl_runs;
DROP TABLE IF EXISTS scan_watermarks;

COMMIT;
