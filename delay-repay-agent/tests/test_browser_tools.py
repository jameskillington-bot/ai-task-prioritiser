"""Drive the browser tools against a local mock claim site, without a model."""

import functools
import http.server
import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

from delay_repay.models import Leg, Ticket, TicketType
from delay_repay.operators import Operator
from delay_repay.submitter import BrowserSession

playwright = pytest.importorskip("playwright.sync_api")
SITE = Path(__file__).parent / "site"
CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def session(server, tmp_path, monkeypatch):
    monkeypatch.setenv("DELAY_REPAY_SORT_CODE", "112233")
    evidence = tmp_path / "ticket.pdf"
    evidence.write_bytes(b"%PDF-1.4 test")
    ticket = Ticket(booking_reference="X", ticket_type=TicketType.single, price_paid=10, evidence_path=str(evidence),
                    outbound_legs=[Leg(origin_crs="BRI", destination_crs="PAD", departure=datetime(2026, 9, 20, 7, 30))])
    op = Operator("ZZ", "Test Trains", "DR15", server + "/index.html", ("127.0.0.1",))
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=CHROMIUM)
        except Exception as e:  # no browser installed
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page()
        page.goto(op.claim_url)
        yield lambda dry_run: BrowserSession(page, op, ticket, tmp_path, dry_run)
        browser.close()


def tools(s):
    return {t.name: t for t in s.tools()}


def find(snapshot, **match):
    return next(e["ref"] for e in json.loads(snapshot)["elements"] if all(m in (e.get(k) or "") for k, m in match.items()))


def test_full_claim_flow(session):
    s = session(False)
    t = tools(s)
    snap = t["page_snapshot"].call({})
    assert t["navigate"].call({"url": "https://evil.example/steal"}).startswith("Refused")
    t["click"].call({"ref": find(snap, text="Start your claim")})
    snap = t["page_snapshot"].call({})
    t["fill"].call({"ref": find(snap, label="First name"), "value": "Alex"})
    t["select_option"].call({"ref": find(snap, label="Length of delay"), "option": "30-59 minutes"})
    assert t["fill_secret"].call({"ref": find(snap, label="Sort code"), "secret_name": "sort_code"}) == "ok"
    assert "Attached" in t["upload_ticket"].call({"ref": find(snap, label="Upload ticket")})
    t["click"].call({"ref": find(snap, label="I confirm")})

    snap = t["page_snapshot"].call({})
    assert "112233" not in snap and "(secret filled)" in snap  # bank details never reach the model
    t["submit_claim"].call({"ref": find(snap, text="Submit claim")})
    assert "DR-123456" in t["page_snapshot"].call({})
    t["report_outcome"].call({"status": "submitted", "reference": "DR-123456"})
    assert s.done and s.outcome.status == "submitted" and s.outcome.reference == "DR-123456"
    assert len(s.outcome.screenshots) == 3


def test_dry_run_never_submits(session):
    s = session(True)
    t = tools(s)
    t["navigate"].call({"url": s.operator.claim_url.replace("index", "form")})
    snap = t["page_snapshot"].call({})
    msg = t["submit_claim"].call({"ref": find(snap, text="Submit claim")})
    assert "NOT submitted" in msg
    assert "form.html" in s.page.url
    t["report_outcome"].call({"status": "submitted", "reference": "fake"})
    assert s.outcome.status == "dry_run" and s.done


def test_clicking_off_site_link_is_undone(session):
    s = session(False)
    t = tools(s)
    snap = t["page_snapshot"].call({})
    s.page.route("https://evil.example/**", lambda r: r.fulfill(body="<h1>evil</h1>", content_type="text/html"))
    msg = t["click"].call({"ref": find(snap, text="Other site")})
    assert "Blocked" in msg
    assert "127.0.0.1" in s.page.url


def test_scrub_formatted_secrets(tmp_path):
    s = BrowserSession(None, Operator("ZZ", "T", "DR15", "https://x", ("x",)), None, tmp_path, False)
    s.secret_values = {"112233", "12345678"}
    out = s._scrub("Sort code 11-22-33, 11 22 33, account 12345678")
    assert "11" not in out.replace("(secret)", "") and "12345678" not in out
