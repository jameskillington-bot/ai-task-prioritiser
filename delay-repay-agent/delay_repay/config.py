"""Configuration loading. Secrets come from environment variables, never the YAML."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class Claimant(BaseModel):
    title: Optional[str] = None
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    address_line1: str
    address_line2: Optional[str] = None
    town: str
    postcode: str


class Payment(BaseModel):
    # Cash methods first: rail travel vouchers are worth less than money.
    preference: list[Literal["bank_transfer", "paypal", "card_refund", "cheque", "voucher"]] = [
        "bank_transfer", "paypal", "card_refund", "cheque",
    ]
    account_name: Optional[str] = None
    paypal_email: Optional[str] = None
    # Sort code and account number are read from env vars DELAY_REPAY_SORT_CODE
    # and DELAY_REPAY_ACCOUNT_NUMBER and typed straight into the form; the model
    # never sees them.


class Commute(BaseModel):
    """A regular season-ticket journey. Only days listed in `travel_log` or,
    if `assume_travel_on` is set, those weekdays, are checked."""

    origin_crs: str
    destination_crs: str
    outbound_departure: str  # "07:32"
    return_departure: Optional[str] = None  # "17:45"
    return_origin_crs: Optional[str] = None
    return_destination_crs: Optional[str] = None
    assume_travel_on: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = []
    operator: Optional[str] = None


class Season(BaseModel):
    ticket_type: Literal["season_weekly", "season_monthly", "season_annual", "season_custom", "flexi_season"]
    price_paid: float
    valid_from: str
    valid_to: str
    season_days: Optional[int] = None
    evidence_path: Optional[str] = None
    photocard_number: Optional[str] = None
    commute: Commute


class Mailbox(BaseModel):
    imap_host: str = "imap.gmail.com"
    username: str
    folder: str = "INBOX"
    lookback_days: int = 30
    # Email yourself a digest (train confirmations, submitted claims) via SMTP
    # with the same account and app password.
    notify: bool = True
    smtp_host: str = "smtp.gmail.com"
    # Password from env var DELAY_REPAY_IMAP_PASSWORD (use an app password).


class Settings(BaseModel):
    claimant: Claimant
    payment: Payment = Payment()
    mailbox: Optional[Mailbox] = None
    seasons: list[Season] = []
    auto_submit: bool = False
    min_claim_amount: float = 0.01
    min_connection_minutes: int = 5
    # Flexible (Anytime/Off-Peak) tickets: trains departing this long before or
    # after the ticket time are checked, and you confirm which one you caught.
    travel_window_before_minutes: int = 60
    travel_window_after_minutes: int = 120
    # Open returns have no booked time; the window is centred on this instead.
    open_return_time: str = "17:30"
    wait_after_arrival_hours: int = 2
    claim_window_days: int = 28
    require_recorded_times: bool = True
    london_stations_extra: list[str] = []
    operators: dict[str, dict] = {}
    model: str = "claude-opus-5"
    headless: bool = True
    chromium_path: Optional[str] = None  # only if Playwright's bundled browser isn't installed
    data_dir: str = "data"
    rtt_base_url: str = "https://api.rtt.io/api/v1"  # legacy API (RTT_USERNAME/RTT_PASSWORD)
    rtt_nextgen_url: str = "https://data.rtt.io"     # next-gen API (RTT_TOKEN)

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


SECRET_ENV = {
    "rtt_token": "RTT_TOKEN",
    "rtt_username": "RTT_USERNAME",
    "rtt_password": "RTT_PASSWORD",
    "imap_password": "DELAY_REPAY_IMAP_PASSWORD",
    "sort_code": "DELAY_REPAY_SORT_CODE",
    "account_number": "DELAY_REPAY_ACCOUNT_NUMBER",
}


def secret(name: str) -> Optional[str]:
    """Look up a named secret: built-ins above, or DELAY_REPAY_SECRET_<NAME>."""
    env = SECRET_ENV.get(name) or f"DELAY_REPAY_SECRET_{name.upper()}"
    return os.environ.get(env)


def load(path: str | Path = "config.yaml") -> Settings:
    with open(path) as f:
        return Settings.model_validate(yaml.safe_load(f))
