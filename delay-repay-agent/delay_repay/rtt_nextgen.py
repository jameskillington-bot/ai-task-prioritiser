"""Client for the Realtime Trains next-generation API (https://data.rtt.io).

Sign up at https://api-portal.rtt.io and put the token it gives you in
RTT_TOKEN. The token may be a long-life access token or a refresh token; a
refresh token is exchanged for an access token automatically.

Responses are converted to the shape the delay analysis in `rtt.py` already
uses (UK-local "HHMM" times with next-day flags), so the analysis doesn't
depend on which API the data came from.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
MIN_INTERVAL = 2.1  # seconds between requests: the base plan allows 30 a minute


class RttAuthError(RuntimeError):
    pass


def _local(ts: str | None) -> datetime | None:
    """ISO 8601 timestamp (UTC, offset or naive UK-local) -> naive UK-local datetime."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(UK).replace(tzinfo=None) if dt.tzinfo else dt


def _hhmm(dt: datetime | None) -> str | None:
    return dt.strftime("%H%M") if dt else None


def _next_day(dt: datetime | None, run_date: str) -> bool:
    return bool(dt) and dt.date() > date.fromisoformat(run_date)


def convert_location(loc: dict, run_date: str) -> dict:
    """One location of a next-gen service -> the legacy location dict."""
    t = loc.get("temporalData") or {}
    arr, dep = t.get("arrival") or {}, t.get("departure") or {}
    codes = (loc.get("location") or {}).get("shortCodes") or [None]
    out: dict = {"crs": codes[0], "description": (loc.get("location") or {}).get("description")}
    if t.get("displayAs") in ("CANCELLED", "DIVERTED"):
        out["displayAs"] = "CANCELLED_CALL"
    else:
        out["displayAs"] = t.get("displayAs") or "PASS"
    for key, block in (("Arrival", arr), ("Departure", dep)):
        booked = _local(block.get("scheduleAdvertised"))
        if booked is None:
            continue  # not an advertised passenger call for this activity
        actual = _local(block.get("realtimeActual"))
        live = actual or _local(block.get("realtimeForecast")) or _local(block.get("realtimeEstimate"))
        out[f"gbttBooked{key}"] = _hhmm(booked)
        out[f"gbttBooked{key}NextDay"] = _next_day(booked, run_date)
        if live:
            out[f"realtime{key}"] = _hhmm(live)
            out[f"realtime{key}NextDay"] = _next_day(live, run_date)
        out[f"realtime{key}Actual"] = actual is not None
        out[f"{key.lower()}Cancelled"] = bool(block.get("isCancelled"))
    return out


class RttNextGenClient:
    def __init__(self, token: str, base_url: str = "https://data.rtt.io"):
        self._token = token.strip()
        self._access: str | None = None
        self._base = base_url.rstrip("/")
        self._cache: dict[str, dict] = {}
        self._last = 0.0

    def _request(self, path: str, bearer: str) -> tuple[int, dict]:
        wait = self._last + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(self._base + path, headers={"Authorization": f"Bearer {bearer}",
                                                                 "Accept": "application/json"})
        for attempt in range(3):
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    return resp.status, (json.loads(body) if body else {})
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(int(e.headers.get("Retry-After") or 30))
                    continue
                return e.code, {}
        return 429, {}

    def _get(self, path: str) -> dict:
        if path in self._cache:
            return self._cache[path]
        status, data = self._request(path, self._access or self._token)
        if status in (401, 403) and self._access is None:
            # Probably a refresh token: swap it for a short-life access token.
            s, tok = self._request("/api/get_access_token", self._token)
            if s == 200 and tok.get("token"):
                self._access = tok["token"]
                status, data = self._request(path, self._access)
        if status in (401, 403):
            raise RttAuthError(f"Realtime Trains rejected the token (HTTP {status}). Check RTT_TOKEN.")
        if status >= 400 and status != 404:
            raise OSError(f"Realtime Trains error HTTP {status} for {path}")
        self._cache[path] = data
        return data

    def info(self) -> dict:
        return self._get("/api/info")

    def search(self, origin: str, destination: str, when: datetime) -> list[dict]:
        q = urllib.parse.urlencode({"code": origin, "filterTo": destination,
                                    "timeFrom": when.strftime("%Y-%m-%dT%H:%M:00")})
        out = []
        for svc in self._get(f"/gb-nr/location?{q}").get("services") or []:
            meta = svc.get("scheduleMetadata") or {}
            op = meta.get("operator") or {}
            out.append({
                "serviceUid": meta.get("identity"),
                "runDate": meta.get("departureDate"),
                "atocCode": op.get("code"),
                "atocName": op.get("name"),
                "isPassenger": meta.get("inPassengerService", True) and meta.get("modeType", "TRAIN") == "TRAIN",
            })
        return [s for s in out if s["serviceUid"] and s["runDate"]]

    def service(self, uid: str, run_date: str) -> dict:
        q = urllib.parse.urlencode({"identity": uid, "departureDate": run_date})
        svc = (self._get(f"/gb-nr/service?{q}").get("service") or {})
        return {"locations": [convert_location(l, run_date) for l in svc.get("locations") or []]}
