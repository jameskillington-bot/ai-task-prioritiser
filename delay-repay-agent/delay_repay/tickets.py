"""Find rail ticket emails and extract structured tickets with Claude."""

from __future__ import annotations

import base64
import email
import imaplib
import logging
import re
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from .config import Settings, secret
from .models import Journey, Leg, Ticket, TicketType
from .stations import is_london

log = logging.getLogger(__name__)

# Senders that issue UK rail e-tickets. Anything else is skipped before any
# model call, so personal email is never sent to the API.
RAIL_SENDER_DOMAINS = (
    "thetrainline.com", "trainline.com", "lner.co.uk", "gwr.com", "avantiwestcoast.co.uk",
    "southeasternrailway.co.uk", "southernrailway.com", "thameslinkrailway.com",
    "greatnorthernrail.com", "gatwickexpress.com", "southwesternrailway.com",
    "greateranglia.co.uk", "c2c-online.co.uk", "chilternrailways.co.uk",
    "eastmidlandsrailway.co.uk", "crosscountrytrains.co.uk", "londonnorthwesternrailway.co.uk",
    "westmidlandsrailway.co.uk", "hulltrains.co.uk", "grandcentralrail.com", "lumo.co.uk",
    "heathrowexpress.com", "tpexpress.co.uk", "northernrailway.co.uk", "tfw.wales",
    "scotrail.co.uk", "sleeper.scot", "splitmyfare.co.uk", "trainpal.com", "raileasy.co.uk",
    "seatfrog.com", "redspottedhanky.com", "virgintrainsticketing.com", "virgin.com", "nationalrail.co.uk", "trainsplit.com",
    "greatbritishrailways.gov.uk",
)


class _ExtractedTicket(BaseModel):
    booking_reference: str
    retailer: Optional[str]
    ticket_type: TicketType
    price_paid: float = Field(description="GBP actually paid for THIS ticket, excluding booking fees")
    passengers: int
    railcard: Optional[str]
    ticket_class: str
    train_specific: bool = Field(description="True only for Advance tickets valid on the booked train alone")
    outbound_legs: list[Leg]
    return_legs: list[Leg]
    season_valid_from: Optional[date]
    season_valid_to: Optional[date]
    season_days: Optional[int]


class _Extraction(BaseModel):
    is_rail_ticket_purchase: bool = Field(description="True only for a confirmed purchase/e-ticket, not marketing or refunds")
    tickets: list[_ExtractedTicket]


EXTRACT_SYSTEM = """You extract UK rail tickets from booking confirmation emails.

Rules:
- Station codes must be 3-letter National Rail CRS codes (London Paddington = PAD, London Euston = EUS, London Kings Cross = KGX, London St Pancras = STP, London Liverpool Street = LST, London Bridge = LBG, London Victoria = VIC, London Waterloo = WAT, London Marylebone = MYB, London Charing Cross = CHX, London Cannon Street = CST, London Fenchurch Street = FST, London Blackfriars = BFR). If the ticket says "London Terminals" use the actual London station of the booked train.
- One leg per booked train. A journey with a change has several legs in order.
- Split tickets: return one ticket per separately-priced ticket, each with only the legs it covers and its own price.
- price_paid is what was paid for that ticket after railcard discount, excluding booking or card fees. For a return, the total return price.
- Times are UK local time as shown. Leave arrival null if not shown.
- Open returns without a booked return train: return_legs is empty.
- train_specific is true only for Advance tickets (valid on the booked train only). Anytime, Off-Peak, Super Off-Peak and season tickets are not train specific.
- Copy values exactly; never guess a price or time that is not in the email. If the email is not a confirmed ticket purchase, set is_rail_ticket_purchase false and tickets empty."""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        if tag in ("br", "p", "tr", "div", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(p.parts)).strip()


def _body_and_attachments(msg: Message) -> tuple[str, list[tuple[str, str, bytes]]]:
    plain, html, files = [], [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        fname = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if fname and ctype in ("application/pdf", "image/png", "image/jpeg"):
            files.append((str(make_header(decode_header(fname))), ctype, payload))
        elif ctype == "text/plain":
            plain.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        elif ctype == "text/html":
            html.append(_html_to_text(payload.decode(part.get_content_charset() or "utf-8", "replace")))
    return ("\n".join(plain) or "\n".join(html)), files


def fetch_ticket_emails(settings: Settings, skip=lambda msg_id: False) -> list[tuple[str, Message]]:
    mb = settings.mailbox
    password = secret("imap_password")
    if not mb or not password:
        log.info("No mailbox configured; skipping email scan.")
        return []
    since = (date.today() - timedelta(days=mb.lookback_days)).strftime("%d-%b-%Y")
    out = []
    with imaplib.IMAP4_SSL(mb.imap_host, timeout=30) as imap:
        imap.login(mb.username, password)
        folder = mb.folder if mb.folder.startswith('"') else f'"{mb.folder}"'
        imap.select(folder, readonly=True)
        _, ids = imap.search(None, "SINCE", since)
        for num in ids[0].split():
            # Headers first, so only rail emails are downloaded in full.
            _, data = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])")
            head = email.message_from_bytes(data[0][1])
            sender = email.utils.parseaddr(head.get("From", ""))[1].lower()
            domain = sender.rsplit("@", 1)[-1]
            if not any(domain == d or domain.endswith("." + d) for d in RAIL_SENDER_DOMAINS):
                continue
            msg_id = (head.get("Message-ID") or f"{mb.username}:{num.decode()}").strip()
            if skip(msg_id):
                continue
            _, data = imap.fetch(num, "(BODY.PEEK[])")
            out.append((msg_id, email.message_from_bytes(data[0][1])))
    return out


def extract_tickets(client: anthropic.Anthropic, settings: Settings, msg_id: str, msg: Message) -> list[Ticket]:
    body, files = _body_and_attachments(msg)
    evidence_dir = settings.data_path / "evidence" / re.sub(r"[^A-Za-z0-9]+", "_", msg_id)[:80]
    saved: list[Path] = []
    content: list[dict] = []
    for fname, ctype, payload in files[:5]:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", fname)
        path.write_bytes(payload)
        saved.append(path)
        b64 = base64.standard_b64encode(payload).decode()
        if ctype == "application/pdf":
            content.append({"type": "document", "source": {"type": "base64", "media_type": ctype, "data": b64}})
        else:
            content.append({"type": "image", "source": {"type": "base64", "media_type": ctype, "data": b64}})
    header = f"From: {msg.get('From')}\nSubject: {make_header(decode_header(msg.get('Subject', '')))}\nDate: {msg.get('Date')}\n\n"
    content.append({"type": "text", "text": header + body[:60000]})

    response = client.beta.messages.parse(
        model=settings.model,
        max_tokens=16000,
        system=EXTRACT_SYSTEM,
        thinking={"type": "adaptive"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
        output_format=_Extraction,
    )
    if response.stop_reason == "refusal":
        log.warning("Extraction declined for %s", msg_id)
        return []
    result = response.parsed_output
    if result is None or not result.is_rail_ticket_purchase:
        return []
    tickets = []
    for t in result.tickets:
        if not t.outbound_legs:
            continue
        data = t.model_dump()
        data["ticket_class"] = "first" if "first" in (t.ticket_class or "").lower() else "standard"
        tickets.append(Ticket(**data, evidence_path=match_evidence(t.outbound_legs, saved), source_message_id=msg_id))
    return tickets


def _code_pos(name: str, crs: str) -> int:
    """Where a station appears in an e-ticket filename like 'eTicket-Passenger1-AHT-LON.pdf'."""
    name = name.upper()
    for code in (crs.upper(), "LON") if is_london(crs) else (crs.upper(),):
        i = name.find(code)
        if i >= 0:
            return i
    return -1


def match_evidence(legs: list[Leg], files: list[Path]) -> Optional[str]:
    """Pick the e-ticket PDF for this part of a split ticket, so the return
    claim uploads the return ticket rather than the outward one."""
    if not files:
        return None
    origin, dest = legs[0].origin_crs, legs[-1].destination_crs
    for f in files:
        o, d = _code_pos(f.name, origin), _code_pos(f.name, dest)
        if 0 <= o < d:
            return str(f)
    return str(files[0])


def involves_london(ticket: Ticket, extra: frozenset[str]) -> bool:
    legs = ticket.outbound_legs + ticket.return_legs
    return any(is_london(l.origin_crs, extra) or is_london(l.destination_crs, extra) for l in legs)


def journeys_for(ticket: Ticket, open_return_time: str = "17:30") -> list[Journey]:
    tid = ticket.ticket_id()
    out = [Journey(ticket_id=tid, direction="outbound", legs=ticket.outbound_legs)]
    if ticket.return_legs:
        out.append(Journey(ticket_id=tid, direction="return", legs=ticket.return_legs))
    elif ticket.ticket_type == TicketType.return_ and not ticket.train_specific:
        # Open return: same route reversed, on the day of travel, time unknown.
        first, last = ticket.outbound_legs[0], ticket.outbound_legs[-1]
        when = datetime.combine(first.departure.date(), datetime.strptime(open_return_time, "%H:%M").time())
        leg = Leg(origin_crs=last.destination_crs, destination_crs=first.origin_crs, departure=when, operator=first.operator)
        out.append(Journey(ticket_id=tid, direction="return", legs=[leg], time_is_estimate=True))
    return out


def season_journeys(settings: Settings, travel_log: dict[str, list[str]], today: date) -> list[tuple[Ticket, list[Journey]]]:
    """Journeys on season tickets for days the claimant travelled.

    `travel_log` maps ISO dates to directions actually travelled ("outbound",
    "return"). Days in `assume_travel_on` are included unless logged as
    skipped (an empty list)."""
    out = []
    weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for s in settings.seasons:
        start = date.fromisoformat(s.valid_from)
        end = min(date.fromisoformat(s.valid_to), today)
        c = s.commute
        base = dict(
            booking_reference=f"SEASON-{s.valid_from}",
            ticket_type=TicketType(s.ticket_type),
            price_paid=s.price_paid,
            season_valid_from=start,
            season_valid_to=date.fromisoformat(s.valid_to),
            season_days=s.season_days,
            evidence_path=s.evidence_path,
        )
        d = max(start, today - timedelta(days=settings.claim_window_days))
        while d <= end:
            iso = d.isoformat()
            directions = travel_log.get(iso)
            if directions is None and weekday[d.weekday()] in c.assume_travel_on:
                directions = ["outbound", "return"]
            for direction in directions or []:
                if direction == "outbound":
                    o, dst, t = c.origin_crs, c.destination_crs, c.outbound_departure
                elif c.return_departure:
                    o = c.return_origin_crs or c.destination_crs
                    dst = c.return_destination_crs or c.origin_crs
                    t = c.return_departure
                else:
                    continue
                leg = Leg(origin_crs=o, destination_crs=dst, operator=c.operator,
                          departure=datetime.combine(d, datetime.strptime(t, "%H:%M").time()))
                # Each commute journey is its own "ticket" row so duplicates are
                # keyed per day and direction; the price is the season's.
                ticket = Ticket(**base, outbound_legs=[leg])
                ticket.booking_reference = f"SEASON-{s.valid_from}-{iso}-{direction}"
                out.append((ticket, [Journey(ticket_id=ticket.ticket_id(), direction=direction, legs=[leg])]))
            d += timedelta(days=1)
    return out
