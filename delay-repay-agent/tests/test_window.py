"""Flexible tickets: every train in the travel window is valued, you confirm which one you caught."""

import json
from datetime import datetime

from delay_repay.models import ClaimStatus, Leg, Ticket, TicketType
from delay_repay.pipeline import assess, confirm_train, open_store
from delay_repay.tickets import journeys_for

from .fake_rtt import FakeRtt, call
from .test_pipeline import settings

NOW = datetime(2026, 9, 21, 12, 0)


def aldershot_return(price=24.0, **kw):
    out = Leg(origin_crs="AHT", destination_crs="WAT", departure=datetime(2026, 9, 20, 7, 30))
    return Ticket(booking_reference="SWR1", ticket_type=TicketType.return_, price_paid=price, outbound_legs=[out], **kw)


def timetable():
    rtt = FakeRtt()
    rtt.add("E", "SW", call("AHT", dep="0620"), call("WAT", arr="0720", rt_arr="0750"))  # outside window (before)
    rtt.add("A", "SW", call("AHT", dep="0640"), call("WAT", arr="0740"))                 # on time
    rtt.add("B", "SW", call("AHT", dep="0710"), call("WAT", arr="0810", rt_arr="0829"))  # 19 late
    rtt.add("C", "SW", call("AHT", dep="0740"), call("WAT", arr="0840", rt_arr="0955"))  # 75 late
    rtt.add("D", "SW", call("AHT", dep="0930", cancelled=True), call("WAT", arr="1030", cancelled=True))
    rtt.add("F", "SW", call("AHT", dep="1000"), call("WAT", arr="1100", rt_arr="1105"))  # D's passengers: 35 late
    rtt.add("G", "SW", call("AHT", dep="0945"), call("WAT", arr="1045", rt_arr="1300"))  # outside window (after)
    return rtt


def setup(tmp_path, ticket):
    s = settings(tmp_path)
    store = open_store(s)
    store.add_ticket(ticket, journeys_for(ticket, s.open_return_time))
    return s, store


def test_window_lists_every_qualifying_train_biggest_refund_first(tmp_path):
    s, store = setup(tmp_path, aldershot_return(train_specific=False))
    row = next(r for r in store.journeys() if json.loads(r["data"])["direction"] == "outbound")
    assert assess(s, store, row, timetable(), NOW) == ClaimStatus.confirm_train
    options = json.loads(store.journey(row["id"])["options"])
    got = [(o["n"], o["booked_departure"][11:16], o["delay_minutes"], o["amount"]) for o in options]
    # Single value is half the £24 return = £12.
    assert got == [(1, "07:40", 75, 12.0), (2, "09:30", 35, 6.0), (3, "07:10", 19, 3.0)]
    assert options[1]["cancelled"]


def test_confirmed_train_is_claimed_not_the_biggest(tmp_path):
    s, store = setup(tmp_path, aldershot_return(train_specific=False))
    row = next(r for r in store.journeys() if json.loads(r["data"])["direction"] == "outbound")
    rtt = timetable()
    assess(s, store, row, rtt, NOW)
    assert confirm_train(s, store, row["id"], 3, rtt, NOW) == ClaimStatus.eligible
    done = store.journey(row["id"])
    assert done["amount"] == 3.0
    assert json.loads(done["delay"])["delay_minutes"] == 19
    assert json.loads(done["data"])["legs"][0]["departure"].endswith("07:10:00")


def test_confirm_none(tmp_path):
    s, store = setup(tmp_path, aldershot_return(train_specific=False))
    row = next(r for r in store.journeys() if json.loads(r["data"])["direction"] == "outbound")
    assess(s, store, row, timetable(), NOW)
    assert confirm_train(s, store, row["id"], None, timetable(), NOW) == ClaimStatus.no_delay


def test_quiet_window_does_not_ask(tmp_path):
    s, store = setup(tmp_path, aldershot_return(train_specific=False))
    rtt = FakeRtt()
    rtt.add("A", "SW", call("AHT", dep="0710"), call("WAT", arr="0810", rt_arr="0818"))
    rtt.add("B", "SW", call("AHT", dep="0740"), call("WAT", arr="0840"))
    row = next(r for r in store.journeys() if json.loads(r["data"])["direction"] == "outbound")
    assert assess(s, store, row, rtt, NOW) == ClaimStatus.no_delay
    assert "worst 8 min" in store.journey(row["id"])["notes"]


def test_advance_ticket_only_checks_booked_train(tmp_path):
    s, store = setup(tmp_path, aldershot_return(train_specific=True))
    row = next(r for r in store.journeys() if json.loads(r["data"])["direction"] == "outbound")
    rtt = FakeRtt()
    rtt.add("X", "SW", call("AHT", dep="0730"), call("WAT", arr="0830", rt_arr="0836"))
    rtt.add("C", "SW", call("AHT", dep="0740"), call("WAT", arr="0840", rt_arr="0955"))
    assert assess(s, store, row, rtt, NOW) == ClaimStatus.no_delay


def test_open_return_gets_a_return_journey():
    js = journeys_for(aldershot_return(train_specific=False), "17:30")
    assert [j.direction for j in js] == ["outbound", "return"]
    back = js[1]
    assert (back.origin, back.destination, back.legs[0].departure.hour, back.time_is_estimate) == ("WAT", "AHT", 17, True)
    assert len(journeys_for(aldershot_return(train_specific=True), "17:30")) == 1


def test_digest_email(tmp_path, monkeypatch):
    from delay_repay import notify

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(host)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, user, pw): sent.append(user)
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("DELAY_REPAY_IMAP_PASSWORD", "app-pw")
    s = settings(tmp_path, mailbox={"username": "me@gmail.com"})
    assert notify.send(s, "Delay Repay: 1 update(s)", ["section"])
    host, user, msg = sent
    assert host == "smtp.gmail.com" and user == "me@gmail.com" and msg["To"] == "me@gmail.com"
    assert "Only confirm a train you were actually on." in msg.get_content()


def test_split_ticket_uploads_matching_pdf(tmp_path):
    from delay_repay.tickets import match_evidence
    out_pdf, back_pdf = tmp_path / "eTicket-Passenger1-AHT-LON.pdf", tmp_path / "eTicket-Passenger1-LON-AHT.pdf"
    files = [out_pdf, back_pdf]
    at = lambda h: datetime(2026, 9, 23, h)
    assert match_evidence([Leg(origin_crs="AHT", destination_crs="WAT", departure=at(7))], files) == str(out_pdf)
    assert match_evidence([Leg(origin_crs="WAT", destination_crs="AHT", departure=at(17))], files) == str(back_pdf)
    assert match_evidence([Leg(origin_crs="BRI", destination_crs="BTH", departure=at(9))], files) == str(out_pdf)


def test_virgin_trains_ticketing_is_a_rail_sender():
    from delay_repay.tickets import RAIL_SENDER_DOMAINS
    domain = "comms.virgintrainsticketing.com"
    assert any(domain == d or domain.endswith("." + d) for d in RAIL_SENDER_DOMAINS)
