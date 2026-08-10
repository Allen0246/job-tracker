"""Plain CLI entrypoint: crawl (each source) -> filter -> digest.

Intentionally scheduler-agnostic: this file contains no scheduling
logic. Run it by hand, from Windows Task Scheduler, or from a cron line
on the Pi later — whichever fires `python -m tracker.run_daily` is fine,
nothing here changes.

Each source's crawl is isolated: one broken adapter (site redesign, an
outage) logs and moves on instead of aborting the run, so a single
failure doesn't also take down every other source's filter/digest pass
for the day — a source that used to return dozens of postings and
returns zero is the failure mode that quietly looks like "nobody is
hiring" if it isn't kept separate from the rest of the pipeline.

Any source can be switched off without touching code — set its
ENABLE_<SOURCE> variable in .env to "false". Unset means enabled, so
adding a new source's toggle never silently disables it for someone who
hasn't set the variable.

Every source additionally gets:
  - its own log file (logs/<source>.log), so its history can be reviewed
    in isolation without wading through every other source's interleaved
    lines in the combined run_daily.log;
  - a row in the crawl_runs table (fetched count, success/failure, and
    whether it was a full scan), so a silent-zero failure can be caught
    by querying history rather than grepping logs.

Logs to both the console and logs/run_daily.log, and always ends with
an unambiguous RUN SUCCEEDED / RUN FAILED line — so a run kicked off
unattended (cron, Task Scheduler) leaves a clear trace of its outcome
instead of silence that looks like it hung.
"""

import logging
import sys

from tracker.algolia import run_algolia_crawl
from tracker.ashby import run_ashby_crawl
from tracker.avature import run_avature_crawl
from tracker.config import PROJECT_ROOT, enabled
from tracker.crawler import run_crawl
from tracker.db import get_connection, record_crawl_run
from tracker.digest import run_digest
from tracker.eightfold import run_eightfold_crawl
from tracker.epam import run_epam_crawl
from tracker.filter import run_filter
from tracker.icims import run_icims_crawl
from tracker.phenom import run_phenom_crawl
from tracker.radancy import run_radancy_crawl
from tracker.successfactors import run_successfactors_crawl
from tracker.workday import run_workday_crawl

log = logging.getLogger(__name__)

# (display name, source key as stored on postings.source, crawl function,
#  logger name whose output also gets its own file, that file's name,
#  .env variable that can disable this source)
_CRAWLERS = (
    ("profession.hu", "profession_hu", run_crawl, "tracker.crawler", "profession_hu.log", "ENABLE_PROFESSION_HU"),
    ("Eightfold", "eightfold", run_eightfold_crawl, "tracker.eightfold", "eightfold.log", "ENABLE_EIGHTFOLD"),
    (
        "SuccessFactors",
        "successfactors",
        run_successfactors_crawl,
        "tracker.successfactors",
        "successfactors.log",
        "ENABLE_SUCCESSFACTORS",
    ),
    ("Radancy", "radancy", run_radancy_crawl, "tracker.radancy", "radancy.log", "ENABLE_RADANCY"),
    ("Ashby", "ashby", run_ashby_crawl, "tracker.ashby", "ashby.log", "ENABLE_ASHBY"),
    ("Workday", "workday", run_workday_crawl, "tracker.workday", "workday.log", "ENABLE_WORKDAY"),
    ("Algolia", "algolia", run_algolia_crawl, "tracker.algolia", "algolia.log", "ENABLE_ALGOLIA"),
    ("Avature", "avature", run_avature_crawl, "tracker.avature", "avature.log", "ENABLE_AVATURE"),
    ("iCIMS", "icims", run_icims_crawl, "tracker.icims", "icims.log", "ENABLE_ICIMS"),
    ("Phenom", "phenom", run_phenom_crawl, "tracker.phenom", "phenom.log", "ENABLE_PHENOM"),
    ("EPAM", "epam", run_epam_crawl, "tracker.epam", "epam.log", "ENABLE_EPAM"),
)


def _configure_logging():
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "run_daily.log", encoding="utf-8"),
        ],
    )

    for _, _, _, logger_name, log_filename, _ in _CRAWLERS:
        handler = logging.FileHandler(log_dir / log_filename, encoding="utf-8")
        handler.setFormatter(fmt)
        logging.getLogger(logger_name).addHandler(handler)


def _record_run(
    source: str, fetched_count: int | None, success: bool, error_message: str | None, full_scan: bool
) -> None:
    conn = get_connection()
    try:
        record_crawl_run(conn, source, fetched_count, success, error_message, full_scan)
    finally:
        conn.close()


def main():
    _configure_logging()
    log.info("=== run_daily starting ===")

    any_crawl_failed = False
    for name, source, run, _, _, enable_var in _CRAWLERS:
        if not enabled(enable_var):
            log.info("%s crawl skipped (disabled via %s)", name, enable_var)
            continue

        log.info("%s crawl starting", name)
        try:
            result = run()
            log.info("%s crawl result: %s", name, result)
            # Sources without an early-stop mode (see radancy.py) always
            # scan everything, so treat them as always full_scan for
            # crawl_runs bookkeeping unless the adapter says otherwise.
            _record_run(
                source,
                result.get("total_seen"),
                success=True,
                error_message=None,
                full_scan=result.get("full_scan", True),
            )
        except Exception as exc:
            log.exception("%s crawl FAILED — continuing with remaining sources", name)
            any_crawl_failed = True
            _record_run(source, None, success=False, error_message=str(exc), full_scan=True)

    try:
        log.info("filter starting")
        log.info("filter result: %s", run_filter())
        log.info("digest starting")
        log.info("digest result: %s", run_digest())
    except Exception:
        log.exception("RUN FAILED")
        raise SystemExit(1)

    if any_crawl_failed:
        log.error("RUN COMPLETED WITH CRAWL FAILURES")
        raise SystemExit(1)

    log.info("RUN SUCCEEDED")


if __name__ == "__main__":
    main()
