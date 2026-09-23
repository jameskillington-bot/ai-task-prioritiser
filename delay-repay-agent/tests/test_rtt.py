from datetime import datetime

from delay_repay.models import Journey, Leg
from delay_repay.rtt import analyse_journey

from .fake_rtt import FakeRtt, call


def J(*legs):
    return Journey(ticket_id="t", direction="outbound", legs=list(legs))


def leg(o, d, dep, arr=None):
    return Leg(origin_crs=o, destination_crs=d, departure=datetime.strptime(f"2026-09-20 {dep}", "%Y-%m-%d %H:%M"),
               arrival=datetime.strptime(f"2026-09-20 {arr}", "%Y-%m-%d %H:%M") if arr else None)


def test_simple_late_arrival():
    rtt = FakeRtt()
    rtt.add("A1", "GW", call("BRI", dep="0730"), call("PAD", arr="0915", rt_arr="0952"))
    r = analyse_journey(J(leg("BRI", "PAD", "07:30")), rtt)
    assert r.delay_minutes == 37
    assert r.responsible_operator_code == "GW"
    assert not r.cancelled and r.confident


def test_cancellation_measured_to_next_train():
    rtt = FakeRtt()
    rtt.add("A1", "GW", call("BRI", dep="0730", cancelled=True), call("PAD", arr="0915", cancelled=True))
    rtt.add("A2", "GW", call("BRI", dep="0800"), call("PAD", arr="0945", rt_arr="0950"))
    r = analyse_journey(J(leg("BRI", "PAD", "07:30", "09:15")), rtt)
    assert r.cancelled
    assert r.delay_minutes == 35
    assert r.legs_taken[0].service_uid == "A2"


def test_missed_connection_counts_full_delay_and_blames_first_operator():
    rtt = FakeRtt()
    rtt.add("S1", "SE", call("CBW", dep="0700"), call("LBG", arr="0730", rt_arr="0748"))
    rtt.add("T1", "TL", call("LBG", dep="0740"), call("ZFD", arr="0750"))
    rtt.add("T2", "TL", call("LBG", dep="0810"), call("ZFD", arr="0820"))
    r = analyse_journey(J(leg("CBW", "LBG", "07:00", "07:30"), leg("LBG", "ZFD", "07:40", "07:50")), rtt)
    # 18 min late into London Bridge, 5 min connection, so 08:10 train, arriving 30 min late.
    assert r.delay_minutes == 30
    assert r.legs_taken[1].service_uid == "T2"
    assert r.responsible_operator_code == "SE"


def test_takes_earliest_arrival_not_longest_delay():
    rtt = FakeRtt()
    rtt.add("A1", "GW", call("BRI", dep="0730", cancelled=True), call("PAD", arr="0915", cancelled=True))
    rtt.add("A2", "GW", call("BRI", dep="0800"), call("PAD", arr="1000", rt_arr="1010"))  # slow stopper
    rtt.add("A3", "GW", call("BRI", dep="0805"), call("PAD", arr="0935"))                 # fast
    r = analyse_journey(J(leg("BRI", "PAD", "07:30")), rtt)
    assert r.legs_taken[0].service_uid == "A3"
    assert r.delay_minutes == 20


def test_predicted_times_flagged():
    rtt = FakeRtt()
    rtt.add("A1", "GW", call("BRI", dep="0730", actual=False), call("PAD", arr="0915", rt_arr="0940", actual=False))
    r = analyse_journey(J(leg("BRI", "PAD", "07:30")), rtt)
    assert not r.confident
