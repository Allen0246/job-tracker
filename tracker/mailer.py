import smtplib
from email.mime.text import MIMEText

from tracker.config import env


def send_email(subject: str, html_body: str) -> None:
    sender = env("GMAIL_ADDRESS", required=True)
    password = env("GMAIL_APP_PASSWORD", required=True)
    to = env("DIGEST_TO", required=True)

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, [to], msg.as_string())
