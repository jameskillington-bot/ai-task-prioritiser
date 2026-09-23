"""Command line entry point: `python -m delay_repay <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime

import anthropic

from . import pipeline
from .config import load, secret
from .models import ClaimStatus, Journey, Leg, Ticket, TicketType
from .rtt import RttClient
from .tickets import journeys_for


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def cmd_run(settings, args):
    for line in pipeline.run(settings, dry_run=args.dry_run, no_submit=args.no_submit):
        print(line)
    cmd_status(settings, args)


def cmd_status(settings, args):
    store = pipeline.open_store(settings)
    rows = store.journeys()
    if not rows:
        print("No journeys yet.")
        return
    total_claimed = sum(r["amount"] or 0 for r in rows if r["status"] == ClaimStatus.submitted.value)
    print(f"\n{'ID':16}  {'Date':10}  {'Route':9}  {'Delay':>5}  {'Amount':>7}  Op  Status")
    for r in rows:
        j = Journey.model_validate_json(r["data"])
        delay = json.loads(r["delay"])["delay_minutes"] if r["delay"] else None
        amount = f"£{r['amount']:.2f}" if r["amount"] else ""
        status = r["status"] + (f" ({r['claim_reference']})" if r["claim_reference"] else "")
        if r["status"] == ClaimStatus.eligible.value and not settings.auto_submit:
            status += " - run `approve`"
        print(f"{r['id']:16}  {j.travel_date}  {j.origin}-{j.destination}  {delay if delay is not None else '':>5}  "
              f"{amount:>7}  {r['operator'] or '':2}  {status}")
        if args.verbose and r["notes"]:
            for n in json.loads(r["notes"]):
                print(f"{'':20}- {n}")
    print(f"\nSubmitted claims total: £{total_claimed:.2f}")


def cmd_approve(settings, args):
    store = pipeline.open_store(settings)
    row = store.journey(args.journey_id)
    if row is None or row["status"] not in (ClaimStatus.eligible.value, ClaimStatus.needs_review.value,
                                           ClaimStatus.needs_human.value, ClaimStatus.failed.value):
        sys.exit("No claimable journey with that id (see `status`).")
    if not row["compensation"]:
        sys.exit("That journey has no compensation due.")
    print(pipeline.submit_one(settings, store, anthropic.Anthropic(), args.journey_id, args.dry_run))


def cmd_add_ticket(settings, args):
    """For paper tickets or bookings not in your mailbox."""
    out = [Leg(origin_crs=args.origin.upper(), destination_crs=args.destination.upper(),
               departure=_dt(args.depart), arrival=_dt(args.arrive) if args.arrive else None, operator=args.operator)]
    back = []
    if args.return_depart:
        back = [Leg(origin_crs=args.destination.upper(), destination_crs=args.origin.upper(),
                    departure=_dt(args.return_depart), arrival=_dt(args.return_arrive) if args.return_arrive else None,
                    operator=args.operator)]
    ticket = Ticket(booking_reference=args.ref, ticket_type=TicketType(args.type), price_paid=args.price,
                    outbound_legs=out, return_legs=back, evidence_path=args.evidence, railcard=args.railcard)
    store = pipeline.open_store(settings)
    added = store.add_ticket(ticket, journeys_for(ticket))
    print(f"Added {added} journey(s).")


def cmd_travelled(settings, args):
    """Record which season-ticket journeys you actually made on a day."""
    log = pipeline.load_travel_log(settings)
    d = args.date or date.today().isoformat()
    if args.none:
        log[d] = []
    elif args.outbound or args.return_:
        log[d] = ["outbound"] * args.outbound + ["return"] * args.return_
    else:
        log[d] = ["outbound", "return"]
    pipeline.travel_log_path(settings).write_text(json.dumps(log, indent=2, sort_keys=True))
    print(f"{d}: {log[d] or 'did not travel'}")


def cmd_set_arrival(settings, args):
    """Use when you actually arrived later than the next available train."""
    store = pipeline.open_store(settings)
    if store.journey(args.journey_id) is None:
        sys.exit("Unknown journey id.")
    store.update(args.journey_id, arrival_override=_dt(args.arrival).isoformat(), status=ClaimStatus.awaiting_travel)
    data = RttClient(secret("rtt_username") or "", secret("rtt_password") or "", settings.rtt_base_url)
    print(pipeline.assess(settings, store, store.journey(args.journey_id), data, datetime.now()).value)


def main(argv=None):
    p = argparse.ArgumentParser(prog="delay_repay", description="Automatic Delay Repay claims for London journeys.")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scan tickets, check delays, submit eligible claims")
    r.add_argument("--dry-run", action="store_true", help="fill forms but stop before the final submit")
    r.add_argument("--no-submit", action="store_true", help="only check delays")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("status", help="list journeys and claims").set_defaults(fn=cmd_status)

    a = sub.add_parser("approve", help="submit one claim now")
    a.add_argument("journey_id")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_approve)

    t = sub.add_parser("add-ticket", help="add a ticket by hand")
    t.add_argument("--from", dest="origin", required=True)
    t.add_argument("--to", dest="destination", required=True)
    t.add_argument("--depart", required=True, help='"YYYY-MM-DD HH:MM"')
    t.add_argument("--arrive")
    t.add_argument("--return-depart")
    t.add_argument("--return-arrive")
    t.add_argument("--type", required=True, choices=[x.value for x in TicketType])
    t.add_argument("--price", type=float, required=True)
    t.add_argument("--ref", required=True)
    t.add_argument("--operator")
    t.add_argument("--railcard")
    t.add_argument("--evidence", help="path to ticket photo/PDF")
    t.set_defaults(fn=cmd_add_ticket)

    tr = sub.add_parser("travelled", help="log a season-ticket travel day")
    tr.add_argument("date", nargs="?")
    tr.add_argument("--outbound", action="store_true")
    tr.add_argument("--return", dest="return_", action="store_true")
    tr.add_argument("--none", action="store_true", help="did not travel")
    tr.set_defaults(fn=cmd_travelled)

    s = sub.add_parser("set-arrival", help="record when you actually arrived")
    s.add_argument("journey_id")
    s.add_argument("arrival", help='"YYYY-MM-DD HH:MM"')
    s.set_defaults(fn=cmd_set_arrival)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    args.fn(load(args.config), args)


if __name__ == "__main__":
    main()
