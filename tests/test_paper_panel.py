"""The paper card must show the scheduler's health, not a spinner.

Desktop: the card in the rail reads ``/api/paper`` and renders the health label,
the last run's figures, and the recent runs newest-first.

Phone: the rail collapses to a status strip, but the card itself stays put and
stays readable — health is the one thing worth seeing without a sidebar.

The server-side shape of the payload is covered by ``tests/test_dashboard.py``.
"""

from __future__ import annotations

import contextlib
import json
import socket
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from quant.dashboard.server import RunIndex, make_handler

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 428, "height": 926}


def _wait(host: str, port: int, deadline: float) -> None:
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"server on {host}:{port} did not answer in time")


def _write(directory: Path, name: str, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _seed_paper(results: Path) -> None:
    paper = results / "paper"
    _write(
        paper,
        "last_run.json",
        {
            "run_id": "BTCUSDT_1d_ema-atr-trend_20261028T140000Z",
            "created_at": "2026-10-28T14:00:00+00:00",
            "success": True,
            "exit_code": 0,
            "timeframe": "1d",
            "run_timestamp_ms": int(time.time() * 1000),
            "freshness_ok": True,
            "data_end_iso": "2026-10-28 00:00:00",
            "trade_count": 3,
            "equity_final": 9967.28,
            "total_return_pct": -0.33,
            "verdict": "Read with care: only 3 trades — too few to be calm about.",
        },
    )
    for run_id, equity in (
        ("BTCUSDT_1d_ema-atr-trend_20261027T070000Z", 9967.28),
        ("BTCUSDT_1d_ema-atr-trend_20261028T070000Z", 10400.0),
    ):
        _write(
            paper,
            f"{run_id}.json",
            {
                "run_id": run_id,
                "created_at": "2026-10-28T07:00:00+00:00",
                "data_end_iso": "2026-10-28 00:00:00",
                "bar_count": 365,
                "trade_count": 3,
                "equity_final": equity,
                "config": {"initial_cash": 10000.0},
                "freshness_ok": True,
                "verdict": "Read with care",
            },
        )


@contextlib.contextmanager
def _dashboard(results: Path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(RunIndex(results)))
    httpd.timeout = 20
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    _wait(host, port, time.time() + 5.0)
    try:
        yield f"http://{host}:{port}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _open(pw, viewport: dict):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport=viewport)
    errors: list[str] = []
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )
    return browser, page, errors


def test_paper_card_shows_health_and_recent_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        results = Path(tmp) / "results"
        _seed_paper(results)

        with _dashboard(results) as url:
            browser, page, errors = _open(pw, DESKTOP)
            try:
                page.goto(url, wait_until="load")

                expect(page.locator("#paperState")).to_have_text("healthy")
                expect(page.locator("#paperState")).to_have_class("paperstate ok")
                expect(page.locator("#paperReason")).to_have_text(
                    "last run successful and data fresh"
                )
                expect(page.locator("#paperMets")).to_contain_text("9,967.28")
                rows = page.locator("#paperRuns .paperrow")
                expect(rows).to_have_count(2)
                expect(rows.nth(0)).to_contain_text("+4.0%")
                expect(rows.nth(1)).to_contain_text("-0.3%")

                # Wide screens start expanded, so the summary's compact rows
                # would just repeat the metrics table and the run list.
                assert page.locator("#paperCard").evaluate("el => el.open")
                assert not page.locator("#paperPeek").is_visible()
                assert not page.locator("#paperMeta").is_visible()
                assert errors == [], errors
            finally:
                browser.close()


def test_paper_card_survives_the_phone_breakpoint() -> None:
    """Below 900px the sidebar goes away; the health card must not."""
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        results = Path(tmp) / "results"
        _seed_paper(results)

        with _dashboard(results) as url:
            browser, page, errors = _open(pw, PHONE)
            try:
                page.goto(url, wait_until="load")

                state = page.locator("#paperState")
                expect(state).to_be_visible()
                expect(state).to_have_text("healthy")

                # Two compact rows by default: health plus the headline figures,
                # then the data-through date and the last run's verdict.
                card = page.locator("#paperCard")
                assert not card.evaluate("el => el.open")
                expect(page.locator("#paperPeek")).to_be_visible()
                expect(page.locator("#paperPeek")).to_contain_text("9,967.28")
                expect(page.locator("#paperWhen")).to_have_text(
                    "data through 2026-10-28"
                )
                expect(page.locator("#paperVerdict")).to_have_text(
                    "Read with care: only 3 trades — too few to be calm about."
                )
                expect(page.locator("#paperVerdict")).to_be_visible()
                assert not page.locator("#paperReason").is_visible()
                assert card.bounding_box()["height"] < 90, card.bounding_box()

                # Tapping it opens the same detail the desktop card shows, and
                # the compact rows step aside so nothing is said twice.
                page.locator(".paperhead").click()
                expect(page.locator("#paperReason")).to_be_visible()
                expect(page.locator("#paperMets")).to_contain_text("2026-10-28")
                expect(page.locator("#paperRuns .paperrow")).to_have_count(2)
                assert not page.locator("#paperMeta").is_visible()

                # The strip must not turn into the full sidebar on a phone: the
                # saved-run list and the rail footer are desktop-only.
                assert not page.locator("#runList").is_visible()
                assert not page.locator("#openGuideAlt").is_visible()

                # It stays inside the viewport instead of overflowing sideways.
                box = card.bounding_box()
                assert box is not None, box
                assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"] + 0.5, box
                assert errors == [], errors
            finally:
                browser.close()
