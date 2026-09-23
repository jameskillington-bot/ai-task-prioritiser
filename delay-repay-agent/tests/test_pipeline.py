from datetime import date, datetime

from delay_repay.config import Settings
from delay_repay.models import ClaimStatus, Leg, Ticket, TicketType
from delay_repay.pipeline import assess, open_store
from delay_repay.tickets import involves_london, journeys_for, season_journeys

from .fake_rtt import FakeRtt, call

NOW = datetime(2026, 9, 21, 12, 0)


def settings(tmp_path, **kw):
    return Settings.model_validate({
        "claimant": {"first_name": "A", "last_name": "B", "email": "a@b.c", "address_line1": "1 St",
                     "town": "Bristol", "postcode": "BS1 1AA"},
        "data_dir": str(tmp_path), **kw,
    })


def return_ticket(train_specific=True):
    out = Leg(origin_crs="BRI", destination_crs="PAD", departure=datetime(2026, 9, 20, 7, 30))
    back = Leg(origin_crs="PAD", destination_crs="BRI", departure=datetime(2026, 9, 20, 17, 30))
    return Ticket(booking_reference="R1", ticket_type=TicketType.return_, price_paid=100.0,
                  outbound_legs=[out], return_legs=[back], train_specific=train_specific)


def rtt_both_ways(out_late, back_late):
    rtt = FakeRtt()
    rtt.add("O", "GW", call("BRI", dep="0730"), call("PAD", arr="0915", rt_arr=out_late))
    rtt.add("B", "GW", call("PAD", dep="1730"), call("BRI", arr="1915", rt_arr=back_late))
    return rtt


def test_return_ticket_both_legs_checked_and_capped(tmp_path):
    s = settings(tmp_path)
    store = open_store(s)
    t = return_ticket()
    store.add_ticket(t, journeys_for(t))
    rtt = rtt_both_ways("1120", "2130")  # 125 and 135 minutes late
    rows = store.journeys()
    statuses = sorted(assess(s, store, r, rtt, NOW).value for r in rows)
    assert statuses == ["below_threshold", "eligible"]
    # 120+ minutes refunds the whole return fare once; never more than the ticket cost.
    assert sorted(r["amount"] for r in store.journeys()) == [0.0, 100.0]


def test_return_ticket_partial_then_full(tmp_path):
    s = settings(tmp_path)
    store = open_store(s)
    t = return_ticket()
    store.add_ticket(t, journeys_for(t))
    rtt = rtt_both_ways("0950", "2130")  # 35 then 135 minutes late
    for r in store.journeys():
        assess(s, store, r, rtt, NOW)
    assert sum(r["amount"] for r in store.journeys()) == 100.0


def test_short_delay_not_claimed(tmp_path):
    s = settings(tmp_path)
    store = open_store(s)
    t = return_ticket()
    store.add_ticket(t, journeys_for(t))
    rtt = rtt_both_ways("0927", "1915")
    statuses = {assess(s, store, r, rtt, NOW) for r in store.journeys()}
    assert statuses == {ClaimStatus.no_delay}


def test_expired_and_not_yet_travelled(tmp_path):
    s = settings(tmp_path)
    store = open_store(s)
    t = return_ticket()
    store.add_ticket(t, journeys_for(t))
    row = store.journeys()[0]
    assert assess(s, store, row, FakeRtt(), datetime(2026, 9, 20, 6, 0)) == ClaimStatus.awaiting_travel
    assert assess(s, store, row, FakeRtt(), datetime(2026, 11, 1)) == ClaimStatus.expired


def test_london_filter():
    t = return_ticket()
    assert involves_london(t, frozenset())
    t2 = t.model_copy(update={"outbound_legs": [Leg(origin_crs="BRI", destination_crs="BTH", departure=datetime(2026, 9, 20, 7))],
                              "return_legs": []})
    assert not involves_london(t2, frozenset())
    assert involves_london(t2, frozenset({"BTH"}))


def test_season_only_counts_days_travelled(tmp_path):
    s = settings(tmp_path, seasons=[{
        "ticket_type": "season_monthly", "price_paid": 400.0, "valid_from": "2026-09-01", "valid_to": "2026-09-30",
        "commute": {"origin_crs": "RDG", "destination_crs": "PAD", "outbound_departure": "07:32",
                    "return_departure": "17:45", "assume_travel_on": ["tue", "wed"]},
    }])
    log = {"2026-09-15": [], "2026-09-18": ["outbound"]}  # skipped a Tuesday, travelled one way on a Friday
    got = season_journeys(s, log, date(2026, 9, 18))
    days = sorted((j.travel_date.isoformat(), j.direction) for _, js in got for j in js)
    assert ("2026-09-15", "outbound") not in days
    assert ("2026-09-16", "outbound") in days and ("2026-09-16", "return") in days
    assert ("2026-09-18", "outbound") in days and ("2026-09-18", "return") not in days
    assert all(t.ticket_type == TicketType.season_monthly for t, _ in got)
