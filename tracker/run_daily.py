"""Plain CLI entrypoint: crawl -> filter -> digest.

Intentionally scheduler-agnostic: this file contains no scheduling
logic. Run it by hand, from Windows Task Scheduler, or from the systemd
timer in deploy/ on the Pi — whichever fires `python -m tracker.run_daily`
is fine, nothing here changes.

Logs to both the console and logs/run_daily.log, and always ends with
an unambiguous RUN SUCCEEDED / RUN FAILED line — so a run kicked off
unattended (cron, Task Scheduler) leaves a clear trace of its outcome
instead of silence that looks like it hung.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from tracker.config import PROJECT_ROOT
from tracker.crawler import run_crawl
from tracker.digest import run_digest
from tracker.filter import run_filter

log = logging.getLogger(__name__)


def _configure_logging():
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Rotated so a long-running Pi doesn't slowly fill its SD card.
            RotatingFileHandler(
                log_dir / "run_daily.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            ),
        ],
    )


def main():
    _configure_logging()
    try:
        log.info("=== run_daily starting ===")
        log.info("crawl starting")
        log.info("crawl result: %s", run_crawl())
        log.info("filter starting")
        log.info("filter result: %s", run_filter())
        log.info("digest starting")
        log.info("digest result: %s", run_digest())
    except Exception:
        log.exception("RUN FAILED")
        raise SystemExit(1)
    else:
        log.info("RUN SUCCEEDED")


if __name__ == "__main__":
    main()
