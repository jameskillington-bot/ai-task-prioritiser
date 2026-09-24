"""Data models shared across the pipeline."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

UK = ZoneInfo("Europe/London")


def uk_local(value: Optional[datetime]) -> Optional[datetime]:
    """All times in this app are naive UK local time; convert any with a timezone."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(UK).replace(tzinfo=None)
    return value


class TicketType(str, Enum):
    single = "single"
    return_ = "return"
    season_weekly = "season_weekly"
    season_monthly = "season_monthly"
    season_annual = "season_annual"
    season_custom = "season_custom"
    flexi_season = "flexi_season"


class Leg(BaseModel):
    """One train the passenger was booked on."""

    origin_crs: str = Field(description="3-letter CRS code of boarding station, e.g. PAD")
    destination_crs: str = Field(description="3-letter CRS code of alighting station")
    departure: datetime = Field(description="Booked departure, local UK time")
    arrival: Optional[datetime] = Field(None, description="Booked arrival, local UK time, if shown")
    operator: Optional[str] = Field(None, description="Train company running this leg, if shown")

    _local_times = field_validator("departure", "arrival")(classmethod(lambda cls, v: uk_local(v)))


class Ticket(BaseModel):
    """A ticket (or one part of a split ticket) extracted from a booking."""

    booking_reference: str
    retailer: Optional[str] = None
    ticket_type: TicketType
    price_paid: float = Field(description="Total price actually paid for this ticket in GBP")
    passengers: int = 1
    railcard: Optional[str] = None
    ticket_class: Literal["standard", "first"] = "standard"
    # Advance tickets are only valid on the booked train. Anytime / Off-Peak
    # tickets can be used on any train, so the time on the email is indicative.
    train_specific: bool = False
    # Legs grouped by direction. For a return ticket, `return_legs` holds the
    # inbound trains (empty if an open return with no booked time).
    outbound_legs: list[Leg]
    return_legs: list[Leg] = Field(default_factory=list)
    season_valid_from: Optional[date] = None
    season_valid_to: Optional[date] = None
    season_days: Optional[int] = Field(None, description="Validity length in days for custom seasons")
    evidence_path: Optional[str] = Field(None, description="Local path to e-ticket PDF/image for upload")
    source_message_id: Optional[str] = None

    def ticket_id(self) -> str:
        first = self.outbound_legs[0] if self.outbound_legs else None
        key = f"{self.booking_reference}|{first.origin_crs if first else ''}|{first.departure.isoformat() if first else ''}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]


class Journey(BaseModel):
    """One direction of travel on a ticket, measured end to end."""

    ticket_id: str
    direction: Literal["outbound", "return"]
    legs: list[Leg]
    # Open return with no booked time: the departure is a placeholder.
    time_is_estimate: bool = False
    # The passenger has said which train they caught; legs now describe it.
    confirmed_train: bool = False

    @property
    def origin(self) -> str:
        return self.legs[0].origin_crs

    @property
    def destination(self) -> str:
        return self.legs[-1].destination_crs

    @property
    def travel_date(self) -> date:
        return self.legs[0].departure.date()

    def journey_id(self) -> str:
        key = f"{self.ticket_id}|{self.direction}|{self.legs[0].departure.isoformat()}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]


class LegOutcome(BaseModel):
    origin_crs: str
    destination_crs: str
    service_uid: str
    operator_code: Optional[str]
    operator_name: Optional[str]
    booked_departure: datetime
    actual_departure: Optional[datetime]
    booked_arrival: datetime
    actual_arrival: datetime
    arrival_is_actual: bool
    was_booked_service: bool
    booked_service_cancelled: bool


class DelayResult(BaseModel):
    journey_id: str
    scheduled_arrival: datetime
    actual_arrival: datetime
    delay_minutes: int
    cancelled: bool
    # Train company whose service introduced the most delay: claim goes here.
    responsible_operator_code: Optional[str]
    responsible_operator_name: Optional[str]
    legs_taken: list[LegOutcome]
    confident: bool = Field(description="False if any arrival time was a prediction, not recorded")
    notes: list[str] = Field(default_factory=list)


class Compensation(BaseModel):
    amount: float
    band: str
    basis: str
    scheme: str
    single_value: float


class TrainOption(BaseModel):
    """A train the passenger may have caught on a flexible ticket, and what it would pay."""

    n: int
    booked_departure: datetime
    booked_arrival: datetime
    actual_arrival: datetime
    delay_minutes: int
    cancelled: bool
    operator_code: Optional[str]
    amount: float
    band: str


class ClaimStatus(str, Enum):
    awaiting_travel = "awaiting_travel"
    confirm_train = "confirm_train"
    no_delay = "no_delay"
    below_threshold = "below_threshold"
    eligible = "eligible"
    needs_review = "needs_review"
    submitted = "submitted"
    needs_human = "needs_human"
    failed = "failed"
    expired = "expired"
