from datetime import datetime

import pytest

from delay_repay.compensation import calculate, single_journey_value
from delay_repay.models import Leg, Ticket, TicketType


def ticket(kind, price, **kw):
    leg = Leg(origin_crs="BRI", destination_crs="PAD", departure=datetime(2026, 9, 1, 7, 30))
    return Ticket(booking_reference="X", ticket_type=kind, price_paid=price, outbound_legs=[leg], **kw)


@pytest.mark.parametrize("delay,expected,band", [
    (14, None, None),
    (15, 10.00, "15-29 min"),
    (29, 10.00, "15-29 min"),
    (30, 20.00, "30-59 min"),
    (60, 40.00, "60-119 min"),
    (119, 40.00, "60-119 min"),
    (120, 40.00, "120+ min"),
])
def test_single_dr15(delay, expected, band):
    c = calculate(ticket(TicketType.single, 40.0), delay, "DR15")
    assert (c.amount if c else None) == expected
    assert (c.band if c else None) == band


def test_dr30_ignores_short_delays():
    assert calculate(ticket(TicketType.single, 40.0), 25, "DR30") is None
    assert calculate(ticket(TicketType.single, 40.0), 30, "DR30").amount == 20.0


def test_return_uses_half_fare_then_full_fare_at_120():
    t = ticket(TicketType.return_, 90.0)
    assert calculate(t, 20, "DR15").amount == 11.25   # 25% of 45
    assert calculate(t, 75, "DR15").amount == 45.00   # 100% of 45
    assert calculate(t, 125, "DR15").amount == 90.00  # whole return fare


@pytest.mark.parametrize("kind,price,value", [
    (TicketType.season_weekly, 100.0, 10.0),
    (TicketType.season_monthly, 400.0, 10.0),
    (TicketType.season_annual, 4640.0, 10.0),
    (TicketType.flexi_season, 160.0, 10.0),
])
def test_season_journey_value(kind, price, value):
    assert single_journey_value(ticket(kind, price))[0] == pytest.approx(value)


def test_custom_season_needs_days():
    with pytest.raises(ValueError):
        single_journey_value(ticket(TicketType.season_custom, 100.0))
    v, _ = single_journey_value(ticket(TicketType.season_custom, 100.0, season_days=365))
    assert v == pytest.approx(100 / 464)
