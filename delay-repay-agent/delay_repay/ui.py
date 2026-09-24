"""A small local web page for reviewing and submitting claims.

Runs on http://127.0.0.1 only. Every request must carry a random token kept in
data/ui_token, so other web pages open in your browser can't drive it. Slow
actions (checking trains, filling in the claim form) run as background jobs
and the page polls for their progress. The server shuts itself down after two
idle hours.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic

from . import pipeline
from .config import Settings
from .models import ClaimStatus, Journey, TrainOption
from .rtt import make_train_data
from .rtt_nextgen import RttRateLimited
from .stations import name

log = logging.getLogger(__name__)

PORT = 8765
IDLE_SHUTDOWN = 2 * 3600
PAGE = Path(__file__).with_name("ui.html")

NEEDS_YOU = {ClaimStatus.confirm_train, ClaimStatus.eligible, ClaimStatus.needs_review,
             ClaimStatus.needs_human, ClaimStatus.failed}


def token_for(settings: Settings) -> str:
    path = settings.data_path / "ui_token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    return path.read_text().strip()


def url_for(settings: Settings, port: int = PORT) -> str:
    return f"http://127.0.0.1:{port}/?t={token_for(settings)}"


# -- view model ---------------------------------------------------------------

def _latest_shot(settings: Settings, jid: str, kind: str) -> str | None:
    folder = settings.data_path / "claims" / jid
    shots = sorted(folder.glob(f"*-{kind}.png")) if folder.exists() else []
    return f"{jid}/{shots[-1].name}" if shots else None


def journey_view(settings: Settings, row) -> dict:
    j = Journey.model_validate_json(row["data"])
    delay = json.loads(row["delay"]) if row["delay"] else None
    comp = json.loads(row["compensation"]) if row["compensation"] else None
    options = [TrainOption.model_validate(o).model_dump(mode="json") for o in json.loads(row["options"] or "[]")]
    status = ClaimStatus(row["status"])
    deadline = j.travel_date + timedelta(days=settings.claim_window_days)
    return {
        "id": row["id"],
        "date": j.travel_date.isoformat(),
        "direction": j.direction,
        "origin": name(j.origin),
        "destination": name(j.destination),
        "departure": j.legs[0].departure.strftime("%H:%M"),
        "confirmed_train": j.confirmed_train,
        "status": status.value,
        "needs_you": status in NEEDS_YOU,
        "amount": row["amount"],
        "operator": row["operator"],
        "delay_minutes": delay["delay_minutes"] if delay else None,
        "scheduled_arrival": delay["scheduled_arrival"][11:16] if delay else None,
        "actual_arrival": delay["actual_arrival"][11:16] if delay else None,
        "cancelled": delay["cancelled"] if delay else False,
        "band": comp["band"] if comp else None,
        "basis": comp["basis"] if comp else None,
        "arrival_override": row["arrival_override"],
        "options": options,
        "notes": json.loads(row["notes"] or "[]"),
        "claim_reference": row["claim_reference"],
        "deadline": deadline.isoformat(),
        "days_left": (deadline - date.today()).days,
        "preview_shot": _latest_shot(settings, row["id"], "before-submit"),
        "final_shot": _latest_shot(settings, row["id"], "submitted"),
    }


def summary(settings: Settings) -> dict:
    store = pipeline.open_store(settings)
    rows = store.journeys()
    views = sorted((journey_view(settings, r) for r in rows), key=lambda v: (v["date"], v["departure"]), reverse=True)
    claimed = sum(v["amount"] or 0 for v in views if v["status"] == ClaimStatus.submitted.value)
    last = settings.data_path / "last_run"
    return {
        "journeys": views,
        "claimed_total": round(claimed, 2),
        "last_checked": last.read_text().strip() if last.exists() else None,
        "auto_submit": settings.auto_submit,
    }


# -- background jobs ------------------------------------------------------------

class Jobs:
    """One job at a time: every action shares the Realtime Trains allowance."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.last: dict | None = None

    def start(self, label: str, journey_id: str | None, fn) -> tuple[bool, str]:
        with self.lock:
            if self.current:
                return False, f"Busy: {self.current['label']}"
            self.current = {"label": label, "journey_id": journey_id, "started": time.time()}

        def work():
            try:
                message = fn() or "Done."
                result = {"ok": True, "message": message}
            except RttRateLimited:
                result = {"ok": False, "message": "Realtime Trains allowance used up for now. Try again in an hour."}
            except anthropic.AuthenticationError:
                result = {"ok": False, "message": "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env."}
            except Exception as e:  # show any failure on the page
                log.exception("job failed")
                result = {"ok": False, "message": f"{type(e).__name__}: {e}"}
            with self.lock:
                self.last = {**self.current, **result, "finished": time.time()}
                self.current = None

        threading.Thread(target=work, daemon=True).start()
        return True, "Started"

    def state(self) -> dict:
        with self.lock:
            return {"current": self.current, "last": self.last}


def _confirm(settings: Settings, jid: str, n: int | None, arrival: str | None) -> str:
    store = pipeline.open_store(settings)
    data = make_train_data(settings)
    status = pipeline.confirm_train(settings, store, jid, n, data, datetime.now())
    if n is not None and arrival:
        row = store.journey(jid)
        day = Journey.model_validate_json(row["data"]).travel_date
        when = datetime.combine(day, datetime.strptime(arrival, "%H:%M").time())
        store.update(jid, arrival_override=when.isoformat(), status=ClaimStatus.awaiting_travel)
        status = pipeline.assess(settings, store, store.journey(jid), data, datetime.now())
    amount = store.journey(jid)["amount"]
    return f"{status.value.replace('_', ' ')}" + (f": £{amount:.2f}" if amount else "")


def _arrival(settings: Settings, jid: str, arrival: str) -> str:
    store = pipeline.open_store(settings)
    row = store.journey(jid)
    day = Journey.model_validate_json(row["data"]).travel_date
    when = datetime.combine(day, datetime.strptime(arrival, "%H:%M").time())
    store.update(jid, arrival_override=when.isoformat(), status=ClaimStatus.awaiting_travel)
    status = pipeline.assess(settings, store, store.journey(jid), make_train_data(settings), datetime.now())
    amount = store.journey(jid)["amount"]
    return f"{status.value.replace('_', ' ')}" + (f": £{amount:.2f}" if amount else "")


def _claim(settings: Settings, jid: str, dry_run: bool) -> str:
    store = pipeline.open_store(settings)
    result = pipeline.submit_one(settings, store, anthropic.Anthropic(), jid, dry_run)
    if dry_run:
        return "Preview ready: check the filled-in form below before submitting."
    ref = store.journey(jid)["claim_reference"]
    return {"submitted": f"Submitted. SWR reference {ref}." if ref else "Submitted."}.get(
        result, f"Not submitted ({result}). See the notes on the card.")


def _check_now(settings: Settings) -> str:
    report = pipeline.run(settings, no_submit=True)
    return report[-1] if report else "Checked."


# -- HTTP -----------------------------------------------------------------------

def make_handler(settings: Settings, token: str, jobs: Jobs, touched: list[float]):
    claims_dir = (settings.data_path / "claims").resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode())

        def _authorised(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            given = self.headers.get("X-Token") or (q.get("t") or [""])[0]
            return secrets.compare_digest(given, token)

        def do_GET(self):
            touched[0] = time.time()
            path = urlparse(self.path).path
            if path == "/ping":
                return self._send(200, b"delay-repay", "text/plain")
            if not self._authorised():
                return self._send(403, b"Open Delay Repay from the app or `./run.sh ui`.", "text/plain")
            if path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/summary":
                return self._json({**summary(settings), "jobs": jobs.state()})
            if path == "/api/jobs":
                return self._json(jobs.state())
            if path.startswith("/shots/"):
                target = (claims_dir / path[len("/shots/"):]).resolve()
                if claims_dir in target.parents and target.suffix == ".png" and target.exists():
                    return self._send(200, target.read_bytes(), "image/png")
                return self._send(404, b"", "text/plain")
            self._send(404, b"", "text/plain")

        def do_POST(self):
            touched[0] = time.time()
            if not self._authorised() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(403, b"", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._json({"ok": False, "message": "Bad request"}, 400)
            path = urlparse(self.path).path
            jid = body.get("id")
            store = pipeline.open_store(settings)
            if jid is not None and store.journey(jid) is None:
                return self._json({"ok": False, "message": "Unknown journey"}, 404)

            if path == "/api/confirm":
                n = body.get("n")
                arrival = (body.get("arrival") or "").strip() or None
                if arrival and not _valid_time(arrival):
                    return self._json({"ok": False, "message": "Arrival time must be HH:MM"}, 400)
                label = "Checking that train" if n is not None else "Updating"
                ok, msg = jobs.start(label, jid, lambda: _confirm(settings, jid, n, arrival))
            elif path == "/api/arrival":
                arrival = (body.get("arrival") or "").strip()
                if not _valid_time(arrival):
                    return self._json({"ok": False, "message": "Arrival time must be HH:MM"}, 400)
                ok, msg = jobs.start("Recalculating with your arrival time", jid, lambda: _arrival(settings, jid, arrival))
            elif path == "/api/preview":
                ok, msg = jobs.start("Filling in the claim form (preview)", jid, lambda: _claim(settings, jid, True))
            elif path == "/api/submit":
                if not _latest_shot(settings, jid, "before-submit"):
                    return self._json({"ok": False, "message": "Preview the claim first."}, 400)
                ok, msg = jobs.start("Submitting the claim", jid, lambda: _claim(settings, jid, False))
            elif path == "/api/check":
                ok, msg = jobs.start("Checking emails and trains", None, lambda: _check_now(settings))
            else:
                return self._send(404, b"", "text/plain")
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)

    return Handler


def _valid_time(s: str) -> bool:
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except ValueError:
        return False


def already_running(port: int = PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            return r.read() == b"delay-repay"
    except OSError:
        return False


def serve(settings: Settings, port: int = PORT, open_browser: bool = True) -> None:
    url = url_for(settings, port)
    if already_running(port):
        if open_browser:
            webbrowser.open(url)
        print(f"Delay Repay is already open: {url}")
        return
    touched = [time.time()]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(settings, token_for(settings), Jobs(), touched))

    def idle_watch():
        while True:
            time.sleep(60)
            if time.time() - touched[0] > IDLE_SHUTDOWN:
                server.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()
    print(f"Delay Repay: {url}\nLeave this running while you use the page (control+C to stop).")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
