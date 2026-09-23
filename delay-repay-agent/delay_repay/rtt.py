"""Realtime Trains client and delay analysis.

Uses the Realtime Trains pull API (https://api.rtt.io, free account). Times in
responses are UK local times as "HHMM" strings with separate next-day flags.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Protocol

from .models import DelayResult, Journey, Leg, LegOutcome

CANCELLED = {"CANCELLED_CALL", "CANCELLED_PASS"}


class TrainData(Protocol):
    def search(self, origin: str, destination: str, when: datetime) -> list[dict]: ...
    def service(self, uid: str, run_date: str) -> dict: ...


class RttClient:
    def __init__(self, username: str, password: str, base_url: str = "https://api.rtt.io/api/v1"):
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._base = base_url.rstrip("/")
        self._cache: dict[str, dict] = {}

    def _get(self, path: str) -> dict:
        if path in self._cache:
            return self._cache[path]
        req = urllib.request.Request(self._base + path, headers={"Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                data = {}
            else:
                raise
        self._cache[path] = data
        return data

    def search(self, origin: str, destination: str, when: datetime) -> list[dict]:
        path = f"/json/search/{origin}/to/{destination}/{when:%Y/%m/%d/%H%M}"
        return self._get(path).get("services") or []

    def service(self, uid: str, run_date: str) -> dict:
        y, m, d = run_date.split("-")
        return self._get(f"/json/service/{uid}/{y}/{m}/{d}")


def _time(run_date: str, hhmm: str | None, next_day: bool = False) -> datetime | None:
    if not hhmm:
        return None
    base = datetime.strptime(run_date, "%Y-%m-%d")
    t = base.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:4]))
    return t + timedelta(days=1) if next_day else t


def _call(detail: dict, crs: str, after_index: int = -1) -> tuple[int, dict] | tuple[None, None]:
    for i, loc in enumerate(detail.get("locations") or []):
        if i > after_index and loc.get("crs") == crs:
            return i, loc
    return None, None


def _is_cancelled(loc: dict) -> bool:
    return loc.get("displayAs") in CANCELLED or bool(loc.get("cancelReasonCode"))


class _Candidate:
    def __init__(self, svc: dict, detail: dict, leg: Leg):
        self.uid = svc["serviceUid"]
        self.run_date = svc["runDate"]
        self.operator_code = svc.get("atocCode")
        self.operator_name = svc.get("atocName")
        oi, o = _call(detail, leg.origin_crs)
        di, d = _call(detail, leg.destination_crs, after_index=oi if oi is not None else -1)
        self.valid = o is not None and d is not None and not _is_cancelled(o) and not _is_cancelled(d)
        self.booked_departure = _time(self.run_date, o and o.get("gbttBookedDeparture"), bool(o and o.get("gbttBookedDepartureNextDay")))
        self.actual_departure = _time(self.run_date, o and o.get("realtimeDeparture"), bool(o and o.get("realtimeDepartureNextDay"))) or self.booked_departure
        self.booked_arrival = _time(self.run_date, d and d.get("gbttBookedArrival"), bool(d and d.get("gbttBookedArrivalNextDay")))
        self.actual_arrival = _time(self.run_date, d and d.get("realtimeArrival"), bool(d and d.get("realtimeArrivalNextDay")))
        self.arrival_is_actual = bool(d and d.get("realtimeArrivalActual"))
        self.origin_cancelled = o is not None and _is_cancelled(o)
        self.destination_cancelled = d is not None and _is_cancelled(d)
        if self.actual_arrival is None and self.valid:
            # No realtime report at the destination: fall back to booked time.
            self.actual_arrival = self.booked_arrival


def _candidates(data: TrainData, leg: Leg, search_from: datetime, cache: dict) -> list[_Candidate]:
    out = []
    for svc in data.search(leg.origin_crs, leg.destination_crs, search_from):
        if svc.get("isPassenger") is False:
            continue
        key = (svc["serviceUid"], svc["runDate"], leg.origin_crs, leg.destination_crs)
        if key not in cache:
            cache[key] = _Candidate(svc, data.service(svc["serviceUid"], svc["runDate"]), leg)
        out.append(cache[key])
    return out


def analyse_journey(
    journey: Journey,
    data: TrainData,
    min_connection_minutes: int = 5,
    max_search_hours: int = 6,
) -> DelayResult:
    """Work out when the passenger actually reached the journey's destination.

    For each leg we take the booked train if it ran and could be caught;
    otherwise (cancellation, missed connection) the earliest-arriving train on
    the same route that departed after the passenger was ready. Choosing the
    earliest possible arrival keeps the claim honest: we never assume the
    passenger waited longer than necessary. Use a manual override if you
    actually arrived later than that.
    """
    notes: list[str] = []
    cache: dict = {}
    outcomes: list[LegOutcome] = []
    ready = journey.legs[0].departure
    confident = True
    scheduled_arrival = None
    any_cancelled = False
    lateness_by_leg: list[tuple[float, LegOutcome]] = []
    prev_lateness = 0.0

    for n, leg in enumerate(journey.legs):
        booked = next(
            (c for c in _candidates(data, leg, leg.departure, cache) if c.booked_departure == leg.departure),
            None,
        )
        leg_booked_arrival = leg.arrival or (booked.booked_arrival if booked else None)
        if leg_booked_arrival is None:
            raise LookupError(f"could not find booked {leg.departure:%H:%M} {leg.origin_crs}->{leg.destination_crs} service")
        booked_cancelled = booked is None or not booked.valid
        if booked_cancelled:
            any_cancelled = True
            notes.append(f"Booked {leg.departure:%H:%M} {leg.origin_crs}->{leg.destination_crs} was cancelled or did not call.")

        best: _Candidate | None = None
        search_from = min(ready, leg.departure)
        deadline = ready + timedelta(hours=max_search_hours)
        while best is None and search_from < deadline:
            usable = [
                c for c in _candidates(data, leg, search_from, cache)
                if c.valid and c.actual_departure and c.actual_departure >= ready and c.actual_arrival
            ]
            if usable:
                best = min(usable, key=lambda c: c.actual_arrival)
            search_from += timedelta(hours=1)
        if best is None:
            raise LookupError(f"no train found {leg.origin_crs}->{leg.destination_crs} within {max_search_hours}h")

        was_booked = booked is not None and best.uid == booked.uid
        if not was_booked and not booked_cancelled:
            notes.append(f"Missed booked {leg.departure:%H:%M} from {leg.origin_crs}; next train {best.booked_departure:%H:%M}.")
        confident &= best.arrival_is_actual
        outcome = LegOutcome(
            origin_crs=leg.origin_crs,
            destination_crs=leg.destination_crs,
            service_uid=best.uid,
            operator_code=best.operator_code or leg.operator,
            operator_name=best.operator_name,
            booked_departure=leg.departure,
            actual_departure=best.actual_departure,
            booked_arrival=leg_booked_arrival,
            actual_arrival=best.actual_arrival,
            arrival_is_actual=best.arrival_is_actual,
            was_booked_service=was_booked,
            booked_service_cancelled=booked_cancelled,
        )
        outcomes.append(outcome)
        lateness = (outcome.actual_arrival - leg_booked_arrival).total_seconds() / 60
        lateness_by_leg.append((lateness - prev_lateness, outcome))
        prev_lateness = lateness
        ready = outcome.actual_arrival + timedelta(minutes=min_connection_minutes)
        if n == len(journey.legs) - 1:
            scheduled_arrival = leg_booked_arrival

    actual_arrival = outcomes[-1].actual_arrival
    delay = max(0, int((actual_arrival - scheduled_arrival).total_seconds() // 60))
    responsible = max(lateness_by_leg, key=lambda x: x[0])[1]
    if not confident:
        notes.append("At least one arrival time is a prediction, not a recorded time.")
    return DelayResult(
        journey_id=journey.journey_id(),
        scheduled_arrival=scheduled_arrival,
        actual_arrival=actual_arrival,
        delay_minutes=delay,
        cancelled=any_cancelled,
        responsible_operator_code=responsible.operator_code,
        responsible_operator_name=responsible.operator_name,
        legs_taken=outcomes,
        confident=confident,
        notes=notes,
    )
