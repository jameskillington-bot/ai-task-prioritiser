"""Delay Repay compensation rules.

Everything here is deterministic: the amount is fixed by the ticket type, the
price paid and the delay at the ticket's destination, so no model is involved.
"Maximising" means applying every rule the passenger is entitled to:

* the right scheme for the operator (DR15 pays from 15 minutes, DR30 from 30);
* a return ticket's single-leg value is half the return fare, but a delay of
  120+ minutes on either leg refunds the whole return fare;
* season tickets use the National Rail per-journey formula;
* a delay caused by a cancellation or missed connection is measured to when the
  passenger actually reached the destination, not to the booked train's time.
"""

from __future__ import annotations

from .models import Compensation, Ticket, TicketType

# (lower bound in minutes, fraction of single value, label)
_BANDS = {
    "DR15": [(120, None, "120+ min"), (60, 1.00, "60-119 min"), (30, 0.50, "30-59 min"), (15, 0.25, "15-29 min")],
    "DR30": [(120, None, "120+ min"), (60, 1.00, "60-119 min"), (30, 0.50, "30-59 min")],
}

# Journeys per season-ticket period used by National Rail to value one journey.
_SEASON_DIVISORS = {
    TicketType.season_weekly: 10,
    TicketType.season_monthly: 40,
    TicketType.season_annual: 464,
    TicketType.flexi_season: 16,  # 8 days of travel, two journeys each
}


def threshold_minutes(scheme: str) -> int:
    return _BANDS[scheme][-1][0]


def single_journey_value(ticket: Ticket) -> tuple[float, str]:
    """Value of one journey on this ticket, and how it was derived."""
    price = ticket.price_paid
    t = ticket.ticket_type
    if t == TicketType.single:
        return price, "single fare paid"
    if t == TicketType.return_:
        return price / 2, "half of the return fare paid"
    if t in _SEASON_DIVISORS:
        d = _SEASON_DIVISORS[t]
        return price / d, f"season price / {d} journeys"
    if t == TicketType.season_custom:
        days = ticket.season_days or 0
        if days <= 0:
            raise ValueError("custom season ticket needs season_days")
        # Pro rata of the annual formula: 464 journeys per 365 days.
        journeys = max(2.0, 464 * days / 365)
        return price / journeys, f"season price / {journeys:.1f} journeys ({days}-day season)"
    raise ValueError(f"unknown ticket type {t}")


def calculate(ticket: Ticket, delay_minutes: int, scheme: str = "DR15") -> Compensation | None:
    """Compensation owed for one journey on `ticket`, or None below threshold."""
    if scheme not in _BANDS:
        raise ValueError(f"unknown scheme {scheme}")
    single, basis = single_journey_value(ticket)
    for lower, fraction, label in _BANDS[scheme]:
        if delay_minutes < lower:
            continue
        if fraction is None:  # 120+ minutes
            if ticket.ticket_type == TicketType.return_:
                amount, basis = ticket.price_paid, "full return fare (delay of 120+ minutes)"
            else:
                amount, basis = single, f"100% of {basis}"
        else:
            amount = single * fraction
            basis = f"{int(fraction * 100)}% of {basis}"
        return Compensation(
            amount=round(amount + 1e-9, 2),
            band=label,
            basis=basis,
            scheme=scheme,
            single_value=round(single, 2),
        )
    return None
