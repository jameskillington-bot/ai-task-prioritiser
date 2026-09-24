"""Command line entry point: `python -m delay_repay <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime

import anthropic

from . import pipeline
from .config import load, secret
from .models import ClaimStatus, Journey, Leg, Ticket, TicketType, TrainOption
from .notify import confirm_request
from .rtt import make_train_data
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
        if r["status"] == ClaimStatus.confirm_train.value:
            amount = "≤" + amount
        print(f"{r['id']:16}  {j.travel_date}  {j.origin}-{j.destination}  {delay if delay is not None else '':>5}  "
              f"{amount:>7}  {r['operator'] or '':2}  {status}")
        if r["status"] == ClaimStatus.confirm_train.value:
            options = [TrainOption.model_validate(o) for o in json.loads(r["options"])]
            deadline = pipeline._deadline(j, settings)
            print("\n".join("      " + line for line in confirm_request(r, j, options, deadline).splitlines()[1:]))
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


def cmd_confirm(settings, args):
    """Say which train you caught on a flexible ticket."""
    store = pipeline.open_store(settings)
    row = store.journey(args.journey_id)
    if row is None or row["status"] != ClaimStatus.confirm_train.value:
        sys.exit("That journey is not waiting for a train confirmation (see `status`).")
    if (args.number is None) == (not args.none):
        sys.exit("Give the option number of the train you caught, or --none.")
    data = make_train_data(settings)
    try:
        status = pipeline.confirm_train(settings, store, args.journey_id, None if args.none else args.number, data, datetime.now())
    except ValueError as e:
        sys.exit(str(e))
    row = store.journey(args.journey_id)
    print(status.value + (f": £{row['amount']:.2f}" if row["amount"] else ""))
    if status == ClaimStatus.eligible and settings.auto_submit:
        print(pipeline.submit_one(settings, store, anthropic.Anthropic(), args.journey_id, dry_run=False))


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
                    outbound_legs=out, return_legs=back, evidence_path=args.evidence, railcard=args.railcard,
                    train_specific=args.advance)
    store = pipeline.open_store(settings)
    added = store.add_ticket(ticket, journeys_for(ticket, settings.open_return_time))
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
    data = make_train_data(settings)
    print(pipeline.assess(settings, store, store.journey(args.journey_id), data, datetime.now()).value)


def cmd_check_rtt(settings, args):
    """Check the Realtime Trains token and show one line-up, e.g. Waterloo -> Aldershot."""
    data = make_train_data(settings)
    if hasattr(data, "info"):
        info = data.info()
        creds = info.get("credentials") or {}
        print(f"API version {info.get('api_version')}; history limit "
              f"{creds.get('historyRestrictToDays') if creds.get('historyRestriction') else 'none'} days")
    when = _dt(args.when) if args.when else datetime.now()
    services = data.search(args.origin.upper(), args.destination.upper(), when)
    print(f"{len(services)} service(s) {args.origin.upper()} -> {args.destination.upper()} from {when:%Y-%m-%d %H:%M}")
    for svc in services[:4]:
        detail = data.service(svc["serviceUid"], svc["runDate"])
        calls = {l["crs"]: l for l in detail.get("locations", [])}
        o, d = calls.get(args.origin.upper(), {}), calls.get(args.destination.upper(), {})
        state = "CANCELLED" if o.get("displayAs") == "CANCELLED_CALL" or o.get("departureCancelled") \
            or d.get("displayAs") == "CANCELLED_CALL" or d.get("arrivalCancelled") else ""
        print(f"  {o.get('gbttBookedDeparture', '----')} dep  -> due {d.get('gbttBookedArrival', '----')}, "
              f"arr {d.get('realtimeArrival', '----')}{' (actual)' if d.get('realtimeArrivalActual') else ''}  "
              f"{svc.get('atocCode') or ''} {state}")


def cmd_check(settings, args):
    """Test each credential and report OK or the problem, without printing secrets."""
    import imaplib

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Anthropic key:   MISSING - add ANTHROPIC_API_KEY to .env")
    elif not key.startswith("sk-ant-api03-"):
        print(f"Anthropic key:   WRONG TYPE - starts {key[:12]!r}; create a Default Workspace API key")
    else:
        try:
            anthropic.Anthropic().models.retrieve(settings.model)
            print(f"Anthropic key:   OK (key ending ...{key[-4:]}, model {settings.model})")
        except anthropic.AuthenticationError:
            print(f"Anthropic key:   REJECTED (key ending ...{key[-4:]}) - create a new key and paste it into .env")
        except anthropic.APIError as e:
            print(f"Anthropic key:   PROBLEM - {e}")

    mb = settings.mailbox
    pw = secret("imap_password")
    if not mb or not pw:
        print("Gmail:           MISSING - mailbox in config.yaml or DELAY_REPAY_IMAP_PASSWORD in .env")
    else:
        try:
            with imaplib.IMAP4_SSL(mb.imap_host, timeout=30) as imap:
                imap.login(mb.username, pw)
            print(f"Gmail:           OK ({mb.username})")
        except (imaplib.IMAP4.error, OSError) as e:
            print(f"Gmail:           FAILED - {e}")

    try:
        data = make_train_data(settings)
        data.info() if hasattr(data, "info") else data.search("WAT", "AHT", datetime.now())
        print("Realtime Trains: OK")
    except Exception as e:  # report any failure rather than crash
        print(f"Realtime Trains: FAILED - {e}")

    sw_user, sw_pw = secret("sw_username"), secret("sw_password")
    print(f"SWR login:       {'OK (' + sw_user + ')' if sw_user and sw_pw else 'MISSING - DELAY_REPAY_SECRET_SW_USERNAME/_PASSWORD in .env'}")
    methods = settings.payment.preference
    if methods and methods[0] == "bank_transfer":
        ok = secret("sort_code") and secret("account_number")
        print(f"Bank details:    {'OK' if ok else 'MISSING - DELAY_REPAY_SORT_CODE / DELAY_REPAY_ACCOUNT_NUMBER in .env'}")


def cmd_setup(settings_path, args):
    """Ask for your details and write a valid config.yaml (SWR Aldershot profile)."""
    import yaml
    from pathlib import Path

    from .config import Settings

    path = Path(settings_path)
    if path.exists() and input(f"{path} exists. Replace it? [y/N] ").strip().lower() != "y":
        sys.exit("Left it unchanged.")
    base = yaml.safe_load(open(Path(__file__).parent.parent / "examples" / "swr-aldershot-waterloo.yaml"))

    def ask(q, default=""):
        a = input(f"{q}{f' [{default}]' if default else ''}: ").strip()
        return a or default

    print("Your details, as they should appear on the claim (press Enter to accept [defaults]):")
    base["claimant"] = {
        "first_name": ask("First name"),
        "last_name": ask("Last name"),
        "email": ask("Email"),
        "phone": ask("Phone"),
        "address_line1": ask("Address line 1 (house number and street)"),
        "address_line2": ask("Address line 2 (optional)") or None,
        "town": ask("Town"),
        "postcode": ask("Postcode").upper(),
    }
    base["mailbox"]["username"] = ask("Gmail address for ticket emails", base["claimant"]["email"])
    method = ask("Refund by bank transfer or PayPal? (bank/paypal)", "bank").lower()
    if method.startswith("p"):
        base["payment"] = {"preference": ["paypal", "bank_transfer", "card_refund", "cheque"],
                           "paypal_email": ask("PayPal email", base["claimant"]["email"])}
    else:
        base["payment"] = {"preference": ["bank_transfer", "paypal", "card_refund", "cheque"],
                           "account_name": ask("Name on bank account",
                                               f"{base['claimant']['first_name']} {base['claimant']['last_name']}")}
    base["headless"] = ask("Show the browser while it fills in claims? (y/n)", "y").lower().startswith("n")
    Settings.model_validate(base)
    path.write_text(yaml.safe_dump(base, sort_keys=False, allow_unicode=True))
    print(f"Wrote {path}. Check it with: cat {path}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="delay_repay", description="Automatic Delay Repay claims for London journeys.")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    # Also accept -v after the command, e.g. `status -v`.
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **kw: _add(*a, parents=[verbose], **kw)

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
    t.add_argument("--advance", action="store_true", help="Advance ticket: valid on the booked train only")
    t.set_defaults(fn=cmd_add_ticket)

    cf = sub.add_parser("confirm", help="say which train you caught (flexible tickets)")
    cf.add_argument("journey_id")
    cf.add_argument("number", nargs="?", type=int, help="option number from `status` or the email")
    cf.add_argument("--none", action="store_true", help="you weren't on any of the listed trains")
    cf.set_defaults(fn=cmd_confirm)

    ck = sub.add_parser("check-rtt", help="test your Realtime Trains token")
    ck.add_argument("--from", dest="origin", default="WAT")
    ck.add_argument("--to", dest="destination", default="AHT")
    ck.add_argument("--when", help='"YYYY-MM-DD HH:MM" (default: now)')
    ck.set_defaults(fn=cmd_check_rtt)

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

    sub.add_parser("setup", help="create config.yaml by answering questions").set_defaults(fn=cmd_setup)
    sub.add_parser("check", help="test your keys and logins").set_defaults(fn=cmd_check)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.fn is cmd_setup:
        return cmd_setup(args.config, args)
    try:
        args.fn(load(args.config), args)
    except anthropic.AuthenticationError:
        sys.exit("The Anthropic API key was rejected. Put a valid key (starting sk-ant-api03-) "
                 "in ANTHROPIC_API_KEY in .env.")


if __name__ == "__main__":
    main()
