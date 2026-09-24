"""The end-to-end run: find journeys, check delays, work out claims, submit."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import anthropic

from . import compensation, notify, submitter
from .config import Settings, secret
from .models import ClaimStatus, Compensation, DelayResult, Journey, Ticket, TrainOption
from .operators import get_operator
from .rtt import TrainData, analyse_journey, analyse_window, make_train_data
from .rtt_nextgen import RttRateLimited
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
    for msg_id, msg in fetch_ticket_emails(settings, skip=store.message_seen):
        if store.message_seen(msg_id):
            continue
        try:
            tickets = extract_tickets(client, settings, msg_id, msg)
        except anthropic.AuthenticationError:
            raise  # a bad key fails every email the same way: stop now
        except anthropic.APIError as e:
            log.warning("Could not read %s: %s", msg_id, e)
            continue
        for t in tickets:
            if involves_london(t, extra):
                added += store.add_ticket(t, journeys_for(t, settings.open_return_time))
        store.mark_message(msg_id)
    for ticket, journeys in season_journeys(settings, load_travel_log(settings), today):
        added += store.add_ticket(ticket, journeys)
    return added


def is_flexible(ticket: Ticket, journey: Journey) -> bool:
    """Could the passenger have caught any train in the travel window?"""
    return not ticket.train_specific and not journey.confirmed_train and len(journey.legs) == 1


def _ready_to_check(journey: Journey, ticket: Ticket, settings: Settings, now: datetime) -> bool:
    last = journey.legs[-1]
    arrival = last.arrival or last.departure + timedelta(hours=3)
    if is_flexible(ticket, journey):
        arrival += timedelta(minutes=settings.travel_window_after_minutes)
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
    if not _ready_to_check(journey, ticket, settings, now):
        return ClaimStatus.awaiting_travel
    if is_flexible(ticket, journey):
        return _assess_window(settings, store, row, journey, ticket, data)

    delay = analyse_journey(journey, data, settings.min_connection_minutes)
    if row["arrival_override"]:
        actual = datetime.fromisoformat(row["arrival_override"])
        delay.actual_arrival = actual
        delay.delay_minutes = max(0, int((actual - delay.scheduled_arrival).total_seconds() // 60))
        delay.confident = True
        delay.notes.append("Actual arrival time entered manually.")

    comp, operator, op_code, scheme, notes = _value(settings, store, row, journey, ticket, delay)
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


def _value(settings: Settings, store: Store, row, journey: Journey, ticket: Ticket, delay: DelayResult):
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
    return comp, operator, op_code, scheme, notes


def _assess_window(settings: Settings, store: Store, row, journey: Journey, ticket: Ticket, data: TrainData) -> ClaimStatus:
    """Flexible ticket: value every train in the window, then ask which one was caught.

    Delay Repay pays for the train you were actually on, so the agent never
    picks the most delayed train by itself. If no train in the window would pay
    anything there is nothing to ask."""
    results = analyse_window(journey, data, settings.travel_window_before_minutes,
                             settings.travel_window_after_minutes, settings.min_connection_minutes)
    if not results:
        raise LookupError(f"no trains found {journey.origin}->{journey.destination} around {journey.legs[0].departure:%H:%M}")
    options = []
    for r in results:
        comp, _, _, _, _ = _value(settings, store, row, journey, ticket, r)
        if comp is not None and comp.amount >= settings.min_claim_amount:
            first = r.legs_taken[0]
            options.append(TrainOption(
                n=0, booked_departure=first.booked_departure, booked_arrival=r.scheduled_arrival,
                actual_arrival=r.actual_arrival, delay_minutes=r.delay_minutes, cancelled=r.cancelled,
                operator_code=r.responsible_operator_code, amount=comp.amount, band=comp.band,
            ))
    worst = max(results, key=lambda r: r.delay_minutes)
    window = (f"{len(results)} trains between {results[0].legs_taken[0].booked_departure:%H:%M} and "
              f"{results[-1].legs_taken[0].booked_departure:%H:%M}")
    if not options:
        store.update(row["id"], status=ClaimStatus.no_delay, options=[],
                     notes=[f"Checked {window}; none qualified (worst {worst.delay_minutes} min late)."])
        return ClaimStatus.no_delay
    # Biggest refund first, so the list is quick to scan; you still pick the one you were on.
    options.sort(key=lambda o: (-o.amount, -o.delay_minutes, o.booked_departure))
    for i, o in enumerate(options, 1):
        o.n = i
    store.update(row["id"], status=ClaimStatus.confirm_train, options=[o.model_dump(mode="json") for o in options],
                 amount=options[0].amount, operator=options[0].operator_code,
                 notes=[f"Checked {window}; {len(options)} would pay. Run `confirm {row['id']} <n>` for the train you caught."])
    return ClaimStatus.confirm_train


def confirm_train(settings: Settings, store: Store, jid: str, n: int | None, data: TrainData, now: datetime) -> ClaimStatus:
    """Record which train the passenger caught (None: none of the listed ones) and value that claim."""
    row = store.journey(jid)
    if n is None:
        store.update(jid, status=ClaimStatus.no_delay, amount=None, notes=["You were not on any of the delayed trains."])
        return ClaimStatus.no_delay
    options = [TrainOption.model_validate(o) for o in json.loads(row["options"] or "[]")]
    chosen = next((o for o in options if o.n == n), None)
    if chosen is None:
        raise ValueError(f"no option {n}; choose from {[o.n for o in options]}")
    journey = Journey.model_validate_json(row["data"])
    leg = journey.legs[0].model_copy(update={"departure": chosen.booked_departure, "arrival": chosen.booked_arrival})
    journey = journey.model_copy(update={"legs": [leg], "confirmed_train": True, "time_is_estimate": False})
    store.update(jid, data=journey, status=ClaimStatus.awaiting_travel, amount=None)
    return assess(settings, store, store.journey(jid), data, now)


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

    digest: list[str] = []
    for row in store.journeys(ClaimStatus.confirm_train):
        if now.date() > _deadline(Journey.model_validate_json(row["data"]), settings):
            store.update(row["id"], status=ClaimStatus.expired, notes=["Train not confirmed before the claim deadline."])

    data = make_train_data(settings)
    for row in store.journeys(ClaimStatus.awaiting_travel):
        try:
            status = assess(settings, store, row, data, now)
        except RttRateLimited as e:
            report.append(str(e))
            break
        except (LookupError, OSError) as e:
            store.update(row["id"], notes=[f"Delay check failed: {e}"])
            log.warning("Delay check failed for %s: %s", row["id"], e)
            continue
        if status != ClaimStatus.awaiting_travel:
            report.append(f"{row['id']}: {status.value}")
        if status == ClaimStatus.confirm_train:
            fresh = store.journey(row["id"])
            journey = Journey.model_validate_json(fresh["data"])
            options = [TrainOption.model_validate(o) for o in json.loads(fresh["options"])]
            digest.append(notify.confirm_request(fresh, journey, options, _deadline(journey, settings)))

    # Oldest first so nothing slips past its deadline.
    for row in sorted(store.journeys(ClaimStatus.eligible), key=lambda r: Journey.model_validate_json(r["data"]).travel_date):
        if no_submit or not (settings.auto_submit or dry_run):
            continue
        result = submit_one(settings, store, client, row["id"], dry_run)
        line = f"{row['id']}: claim {result} (£{row['amount']:.2f} from {row['operator']})"
        report.append(line)
        if result != "dry_run":
            digest.append(line + (f"\n  {json.loads(store.journey(row['id'])['notes'])[-1]}" if result != "submitted" else ""))

    if digest and notify.send(settings, f"Delay Repay: {len(digest)} update(s)", digest):
        report.append("Emailed digest.")
    return report
