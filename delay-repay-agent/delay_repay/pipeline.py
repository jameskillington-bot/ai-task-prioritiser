"""The end-to-end run: find journeys, check delays, work out claims, submit."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import anthropic

from . import compensation, submitter
from .config import Settings, secret
from .models import ClaimStatus, Compensation, DelayResult, Journey
from .operators import get_operator
from .rtt import RttClient, TrainData, analyse_journey
from .store import Store
from .tickets import extract_tickets, fetch_ticket_emails, involves_london, journeys_for, season_journeys

log = logging.getLogger(__name__)


def open_store(settings: Settings) -> Store:
    return Store(settings.data_path / "state.db")


def travel_log_path(settings: Settings):
    return settings.data_path / "travel_log.json"


def load_travel_log(settings: Settings) -> dict[str, list[str]]:
    p = travel_log_path(settings)
    return json.loads(p.read_text()) if p.exists() else {}


def ingest(settings: Settings, store: Store, client: anthropic.Anthropic, today: date) -> int:
    extra = frozenset(s.upper() for s in settings.london_stations_extra)
    added = 0
    for msg_id, msg in fetch_ticket_emails(settings):
        if store.message_seen(msg_id):
            continue
        try:
            tickets = extract_tickets(client, settings, msg_id, msg)
        except anthropic.APIError as e:
            log.warning("Could not read %s: %s", msg_id, e)
            continue
        for t in tickets:
            if involves_london(t, extra):
                added += store.add_ticket(t, journeys_for(t))
        store.mark_message(msg_id)
    for ticket, journeys in season_journeys(settings, load_travel_log(settings), today):
        added += store.add_ticket(ticket, journeys)
    return added


def _ready_to_check(journey: Journey, settings: Settings, now: datetime) -> bool:
    last = journey.legs[-1]
    arrival = last.arrival or last.departure + timedelta(hours=3)
    return now >= arrival + timedelta(hours=settings.wait_after_arrival_hours)


def _deadline(journey: Journey, settings: Settings) -> date:
    return journey.travel_date + timedelta(days=settings.claim_window_days)


def assess(settings: Settings, store: Store, row, data: TrainData, now: datetime) -> ClaimStatus:
    """Check one journey's delay and decide what it is worth."""
    journey = Journey.model_validate_json(row["data"])
    ticket = store.ticket(row["ticket_id"])
    if now.date() > _deadline(journey, settings):
        store.update(row["id"], status=ClaimStatus.expired, notes=["Past the claim deadline."])
        return ClaimStatus.expired
    if not _ready_to_check(journey, settings, now):
        return ClaimStatus.awaiting_travel

    delay = analyse_journey(journey, data, settings.min_connection_minutes)
    if row["arrival_override"]:
        actual = datetime.fromisoformat(row["arrival_override"])
        delay.actual_arrival = actual
        delay.delay_minutes = max(0, int((actual - delay.scheduled_arrival).total_seconds() // 60))
        delay.confident = True
        delay.notes.append("Actual arrival time entered manually.")

    op_code = delay.responsible_operator_code or journey.legs[0].operator
    operator = get_operator(op_code, settings.operators)
    scheme = operator.scheme if operator else "DR15"
    comp = compensation.calculate(ticket, delay.delay_minutes, scheme)
    notes = list(delay.notes)

    if comp is not None:
        # A return ticket can't pay out more than it cost, e.g. 120+ minute
        # delays in both directions.
        remaining = round(ticket.price_paid - store.claimed_total(row["ticket_id"], row["id"]), 2)
        if comp.amount > remaining:
            notes.append(f"Capped at £{remaining:.2f}: the rest of this ticket's value is already claimed.")
            comp = comp.model_copy(update={"amount": max(0.0, remaining)})

    if comp is None or comp.amount < settings.min_claim_amount:
        status = ClaimStatus.no_delay if delay.delay_minutes < 15 else ClaimStatus.below_threshold
        if 10 <= delay.delay_minutes < compensation.threshold_minutes(scheme):
            notes.append(f"Arrived {delay.delay_minutes} min late: just under the {scheme} threshold.")
    elif operator is None:
        status = ClaimStatus.needs_human
        notes.append(f"Unknown operator {op_code!r}: add it under `operators:` in config.")
    elif not delay.confident and settings.require_recorded_times:
        status = ClaimStatus.needs_review
        notes.append("Delay is based on predicted times. Check it and run `approve` or `set-arrival`.")
    else:
        status = ClaimStatus.eligible
    if delay.delay_minutes >= 60 or delay.cancelled:
        notes.append("If this cost you extra (taxi, hotel, missed booking) you may also claim those "
                     "costs from the operator under the Consumer Rights Act 2015.")

    store.update(
        row["id"], status=status, delay=delay, compensation=comp,
        amount=comp.amount if comp else None, operator=operator.code if operator else op_code, notes=notes,
    )
    return status


def submit_one(settings: Settings, store: Store, client: anthropic.Anthropic, jid: str, dry_run: bool) -> str:
    row = store.journey(jid)
    journey = Journey.model_validate_json(row["data"])
    ticket = store.ticket(row["ticket_id"])
    delay = DelayResult.model_validate_json(row["delay"])
    comp = Compensation.model_validate_json(row["compensation"])
    operator = get_operator(row["operator"], settings.operators)
    if date.today() > _deadline(journey, settings):
        store.update(jid, status=ClaimStatus.expired)
        return "expired"
    outcome = submitter.submit(client, settings, operator, ticket, journey, delay, comp, dry_run=dry_run)
    notes = json.loads(row["notes"] or "[]") + [f"{datetime.now():%Y-%m-%d %H:%M} {outcome.status}: {outcome.message}"]
    status = {"submitted": ClaimStatus.submitted, "needs_human": ClaimStatus.needs_human}.get(outcome.status)
    if outcome.status == "dry_run":
        store.update(jid, notes=notes)
    else:
        store.update(jid, status=status or ClaimStatus.failed, claim_reference=outcome.reference, notes=notes)
    return outcome.status


def run(settings: Settings, dry_run: bool = False, no_submit: bool = False) -> list[str]:
    store = open_store(settings)
    client = anthropic.Anthropic()
    now = datetime.now()
    added = ingest(settings, store, client, now.date())
    report = [f"Found {added} new journey(s)."]

    data = RttClient(secret("rtt_username") or "", secret("rtt_password") or "", settings.rtt_base_url)
    for row in store.journeys(ClaimStatus.awaiting_travel):
        try:
            status = assess(settings, store, row, data, now)
        except (LookupError, OSError) as e:
            store.update(row["id"], notes=[f"Delay check failed: {e}"])
            log.warning("Delay check failed for %s: %s", row["id"], e)
            continue
        if status != ClaimStatus.awaiting_travel:
            report.append(f"{row['id']}: {status.value}")

    # Oldest first so nothing slips past its deadline.
    for row in sorted(store.journeys(ClaimStatus.eligible), key=lambda r: Journey.model_validate_json(r["data"]).travel_date):
        if no_submit or not (settings.auto_submit or dry_run):
            continue
        result = submit_one(settings, store, client, row["id"], dry_run)
        report.append(f"{row['id']}: claim {result} (£{row['amount']:.2f} from {row['operator']})")
    return report
