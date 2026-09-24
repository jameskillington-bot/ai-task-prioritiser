"""The local Delay Repay page: auth, views and actions (pipeline calls stubbed)."""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import pytest

from delay_repay import ui
from delay_repay.models import ClaimStatus, Leg, Ticket, TicketType, TrainOption
from delay_repay.pipeline import open_store
from delay_repay.tickets import journeys_for

from .test_pipeline import settings


def seed(s):
    store = open_store(s)
    t = Ticket(booking_reference="B-VIRGINTT-ZLO0Q3G8Q", ticket_type=TicketType.single, price_paid=13.45,
               outbound_legs=[Leg(origin_crs="WAT", destination_crs="AHT", departure=datetime(2026, 9, 23, 17, 25),
                                  arrival=datetime(2026, 9, 23, 18, 7))])
    store.add_ticket(t, journeys_for(t))
    jid = store.journeys()[0]["id"]
    opts = [TrainOption(n=1, booked_departure=datetime(2026, 9, 23, 17, 25), booked_arrival=datetime(2026, 9, 23, 18, 7),
                        actual_arrival=datetime(2026, 9, 23, 19, 24), delay_minutes=77, cancelled=True,
                        operator_code="SW", amount=13.45, band="60-119 min").model_dump(mode="json")]
    store.update(jid, status=ClaimStatus.confirm_train, options=opts, amount=13.45, operator="SW")
    return store, jid


@pytest.fixture
def server(tmp_path, monkeypatch):
    s = settings(tmp_path)
    store, jid = seed(s)
    calls = []
    monkeypatch.setattr(ui, "_confirm", lambda st, j, n, a: calls.append(("confirm", j, n, a)) or "eligible: £6.73")
    monkeypatch.setattr(ui, "_claim", lambda st, j, dry: calls.append(("claim", j, dry)) or "ok")
    token = ui.token_for(s)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(s, token, ui.Jobs(), [time.time()]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", token, jid, calls, s
    srv.shutdown()


def req(url, token=None, body=None, ctype="application/json"):
    headers = {"X-Token": token} if token else {}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), ctype
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers)) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_idle(base, token):
    for _ in range(50):
        if not json.loads(req(base + "/api/jobs", token)[1])["current"]:
            return
        time.sleep(0.05)


def test_token_required(server):
    base, token, jid, calls, s = server
    assert req(base + "/api/summary")[0] == 403
    assert req(base + "/api/summary", "wrong")[0] == 403
    assert req(base + "/?t=" + token)[0] == 200
    assert req(base + "/api/confirm", token, {"id": jid, "n": 1}, ctype="text/plain")[0] == 403  # blocks form posts
    assert calls == []


def test_summary_shows_confirm_card(server):
    base, token, jid, calls, s = server
    data = json.loads(req(base + "/api/summary", token)[1])
    j = data["journeys"][0]
    assert (j["status"], j["origin"], j["destination"], j["needs_you"]) == ("confirm_train", "London Waterloo", "Aldershot", True)
    assert j["options"][0]["cancelled"] and j["options"][0]["amount"] == 13.45


def test_confirm_with_arrival_runs_as_job(server):
    base, token, jid, calls, s = server
    code, body = req(base + "/api/confirm", token, {"id": jid, "n": 1, "arrival": "18:42"})
    assert code == 200
    wait_idle(base, token)
    assert calls == [("confirm", jid, 1, "18:42")]
    assert json.loads(req(base + "/api/jobs", token)[1])["last"]["message"] == "eligible: £6.73"


def test_bad_input_rejected(server):
    base, token, jid, calls, s = server
    assert req(base + "/api/confirm", token, {"id": jid, "n": 1, "arrival": "6.42pm"})[0] == 400
    assert req(base + "/api/confirm", token, {"id": "nope", "n": 1})[0] == 404
    assert req(base + "/api/submit", token, {"id": jid})[0] == 400  # must preview first
    assert calls == []


def test_submit_allowed_after_preview_and_screenshots_served(server):
    base, token, jid, calls, s = server
    shots = s.data_path / "claims" / jid
    shots.mkdir(parents=True)
    (shots / "120000-before-submit.png").write_bytes(b"\x89PNG fake")
    assert req(f"{base}/shots/{jid}/120000-before-submit.png?t={token}")[0] == 200
    assert req(f"{base}/shots/../../state.db?t={token}")[0] == 404
    assert req(base + "/api/submit", token, {"id": jid})[0] == 200
    wait_idle(base, token)
    assert calls == [("claim", jid, False)]
