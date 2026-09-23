"""Email yourself a digest of what needs your input and what was claimed."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings, secret
from .models import Journey, TrainOption

log = logging.getLogger(__name__)


def confirm_request(row, journey: Journey, options: list[TrainOption], deadline) -> str:
    lines = [
        f"{journey.origin} -> {journey.destination}, {journey.travel_date:%a %d %b} ({journey.direction})"
        + (" - open return, time estimated" if journey.time_is_estimate else ""),
        f"Which train did you catch? Claim by {deadline:%d %b}.",
    ]
    for o in options:
        state = "cancelled, " if o.cancelled else ""
        lines.append(f"  {o.n}. {o.booked_departure:%H:%M} (due {o.booked_arrival:%H:%M}, arrived {o.actual_arrival:%H:%M}, "
                     f"{state}{o.delay_minutes} min late)  £{o.amount:.2f}")
    lines.append(f"  ./run.sh confirm {row['id']} <number>     or     ./run.sh confirm {row['id']} --none")
    return "\n".join(lines)


def send(settings: Settings, subject: str, sections: list[str]) -> bool:
    mb = settings.mailbox
    password = secret("imap_password")
    if not sections or not mb or not mb.notify or not password:
        return False
    msg = EmailMessage()
    msg["From"] = mb.username
    msg["To"] = mb.username
    msg["Subject"] = subject
    msg.set_content("\n\n".join(sections) + "\n\nOnly confirm a train you were actually on.\n")
    try:
        with smtplib.SMTP_SSL(mb.smtp_host, 465, timeout=30) as smtp:
            smtp.login(mb.username, password)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.warning("Could not send digest: %s", e)
        return False
