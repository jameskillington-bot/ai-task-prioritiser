"""Next-gen Realtime Trains client against a fake data.rtt.io speaking the published spec."""

import http.server
import json
import threading
import urllib.parse
from datetime import datetime

import pytest

from delay_repay import rtt_nextgen
from delay_repay.models import Journey, Leg
from delay_repay.rtt import analyse_journey, analyse_window
from delay_repay.rtt_nextgen import RttAuthError, RttNextGenClient


def temporal(sched=None, actual=None, forecast=None, cancelled=False):
    t = {"scheduleAdvertised": sched, "isCancelled": cancelled}
    if actual:
        t["realtimeActual"] = actual
    if forecast:
        t["realtimeForecast"] = forecast
    return t


def loc(crs, arr=None, dep=None, display="CALL"):
    return {"location": {"description": crs, "shortCodes": [crs], "longCodes": []},
            "temporalData": {"arrival": arr, "departure": dep, "displayAs": display}}


# Wed 23 Sep 2026 is BST (UTC+1): times below are UTC ("Z").
SERVICES = {
    "W1725": [loc("WAT", dep=temporal("2026-09-23T16:25:00Z", cancelled=True), display="CANCELLED"),
              loc("AHT", arr=temporal("2026-09-23T17:07:00Z", cancelled=True), display="CANCELLED")],
    "W1755": [loc("WAT", dep=temporal("2026-09-23T16:55:00Z", actual="2026-09-23T16:56:00Z")),
              loc("WOK", arr=temporal("2026-09-23T17:20:00Z", actual="2026-09-23T17:22:00Z"),
                  dep=temporal("2026-09-23T17:21:00Z", actual="2026-09-23T17:23:00Z")),
              loc("AHT", arr=temporal("2026-09-23T17:37:00Z", actual="2026-09-23T17:44:00Z"))],
    "W1825": [loc("WAT", dep=temporal("2026-09-23T17:25:00Z", actual="2026-09-23T17:25:00Z")),
              loc("AHT", arr=temporal("2026-09-23T18:07:00Z", actual="2026-09-23T18:09:00Z"))],
}
ACCESS = "access-123"
REFRESH = "refresh-abc"


class Handler(http.server.BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def reply(self, code, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        auth = self.headers.get("Authorization", "")
        Handler.calls.append((url.path, q, auth))
        if url.path == "/api/get_access_token":
            return self.reply(200, {"token": ACCESS}) if auth == f"Bearer {REFRESH}" else self.reply(401)
        if auth != f"Bearer {ACCESS}":
            return self.reply(401)
        if url.path == "/api/info":
            return self.reply(200, {"api_version": "2026-09-01", "credentials": {"historyRestriction": True, "historyRestrictToDays": 14}})
        if url.path == "/gb-nr/location":
            start = datetime.fromisoformat(q["timeFrom"])
            out = []
            for uid, locs in SERVICES.items():
                crs = [l["location"]["shortCodes"][0] for l in locs]
                if q["code"] in crs and q.get("filterTo") in crs:
                    dep = rtt_nextgen._local(locs[crs.index(q["code"])]["temporalData"]["departure"]["scheduleAdvertised"])
                    if 0 <= (dep - start).total_seconds() < 3600:
                        out.append({"scheduleMetadata": {"identity": uid, "departureDate": "2026-09-23",
                                                         "operator": {"code": "SW", "name": "South Western Railway"},
                                                         "inPassengerService": True, "modeType": "TRAIN"}})
            return self.reply(200, {"services": out}) if out else self.reply(204)
        if url.path == "/gb-nr/service":
            return self.reply(200, {"service": {"locations": SERVICES[q["identity"]]}})
        self.reply(404)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(rtt_nextgen, "MIN_INTERVAL", 0)
    Handler.calls = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def journey(dep="17:25", arr="18:07"):
    d = lambda t: datetime.strptime(f"2026-09-23 {t}", "%Y-%m-%d %H:%M")
    return Journey(ticket_id="t", direction="outbound",
                   legs=[Leg(origin_crs="WAT", destination_crs="AHT", departure=d(dep), arrival=d(arr))])


def test_converts_utc_to_uk_local_and_flags_cancellation(api):
    c = RttNextGenClient(ACCESS, api)
    detail = c.service("W1725", "2026-09-23")
    wat, aht = detail["locations"]
    assert wat["gbttBookedDeparture"] == "1725" and wat["displayAs"] == "CANCELLED_CALL" and wat["departureCancelled"]
    assert aht["gbttBookedArrival"] == "1807"
    ok = c.service("W1755", "2026-09-23")["locations"][-1]
    assert ok["realtimeArrival"] == "1844" and ok["realtimeArrivalActual"] is True


def test_cancelled_1725_measured_to_next_train(api):
    r = analyse_journey(journey(), RttNextGenClient(ACCESS, api))
    assert r.cancelled
    assert r.legs_taken[0].service_uid == "W1755"
    assert r.actual_arrival == datetime(2026, 9, 23, 18, 44)
    assert r.delay_minutes == 37 and r.confident and r.responsible_operator_code == "SW"


def test_window_search_over_next_gen(api):
    results = analyse_window(journey(), RttNextGenClient(ACCESS, api), 60, 120)
    by_dep = {r.legs_taken[0].booked_departure.strftime("%H:%M"): r.delay_minutes for r in results}
    assert by_dep == {"17:25": 37, "17:55": 7, "18:25": 2}


def test_refresh_token_is_exchanged_first(api):
    c = RttNextGenClient(REFRESH, api)
    assert c.info()["api_version"] == "2026-09-01"
    c.service("W1755", "2026-09-23")
    paths = [p for p, _, _ in Handler.calls]
    assert paths == ["/api/get_access_token", "/api/info", "/gb-nr/service"]


def test_long_life_access_token_used_directly(api):
    c = RttNextGenClient(ACCESS, api)
    c.info()
    c._cache.clear()
    c.info()
    paths = [p for p, _, _ in Handler.calls]
    assert paths == ["/api/get_access_token", "/api/info", "/api/info"]  # one failed exchange, then direct


def test_searches_are_hour_bucketed_and_cached(api):
    c = RttNextGenClient(ACCESS, api)
    c.search("WAT", "AHT", datetime(2026, 9, 23, 17, 25))
    c.search("WAT", "AHT", datetime(2026, 9, 23, 17, 55))
    searches = [q["timeFrom"] for p, q, _ in Handler.calls if p == "/gb-nr/location"]
    assert searches == ["2026-09-23T17:00:00"]


def test_hourly_allowance_exhausted_stops_cleanly(api, monkeypatch):
    from delay_repay.rtt_nextgen import RttRateLimited

    def limited(self):
        self.send_response(429)
        self.send_header("Retry-After", "1800")
        self.send_header("content-length", "0")
        self.end_headers()
    monkeypatch.setattr(Handler, "do_GET", limited)
    with pytest.raises(RttRateLimited):
        RttNextGenClient(ACCESS, api).search("WAT", "AHT", datetime(2026, 9, 23, 17))


def test_bad_token_is_a_clear_error(api):
    with pytest.raises(RttAuthError):
        RttNextGenClient("wrong", api).info()
