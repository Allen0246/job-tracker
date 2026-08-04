"""Standalone SMTP connectivity check. Sends a trivial fixed message —
does not touch postings, sent_log, or the real digest logic. Useful
whenever you need to confirm mail credentials still work (after a
password rotation, moving to the Pi, etc.) without disturbing dedup
state.
"""

import logging

from tracker.mailer import send_email

log = logging.getLogger(__name__)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    send_email(
        "profession-hu-tracker: test email",
        "<p>This is a test message confirming SMTP delivery works.</p>",
    )
    log.info("test email sent successfully")
