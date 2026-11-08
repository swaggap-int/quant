#!/usr/bin/env python
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from quant.dashboard.server import RunIndex, make_handler, RESULTS_ROOT

URL = "http://127.0.0.1:{port}/index.html"
PHONE = {"width": 428, "height": 926}


def _wait(host: str, port: int, deadline: float) -> None:
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"server on {host}:{port} did not answer in time")


def _run_server(results_dir: Path, host: str, port: int):
    results = results_dir / "results"
    import quant.dashboard.server as srv

    srv.RESULTS_ROOT = results
    srv._EXCESS_SOURCES = (
        results / "status.json",
        results / "explore_notes.json",
        results / "robustness.json",
    )

    httpd = ThreadingHTTPServer((host, port), make_handler(RunIndex(results)))
    httpd.timeout = 20
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait(httpd.server_address[0], httpd.server_address[1], time.time() + 5.0)
    return httpd, httpd.server_address


def _payload() -> dict:
    return {
        "paper": {
            "symbol": "BTCUSDT",
            "timeframe": "1d",
            "start": "2022-01-01 00:00:00",
            "end": "2026-10-16 00:00:00",
            "file": "BTCUSDT_1d_ema-atr-trend_20261016T100000Z.json",
            "trades": 17,
            "total_return_pct": 18.53,
            "buy_hold_return_pct": 180.13,
            "excess_return_pct": -161.59,
            "sharpe": 0.6,
            "max_drawdown_pct": -7.16,
            "fees_pct_of_start": 0.9597012574499365,
            "bars_in_market_pct": 17.06855791962175,
            "verdict": "Read with care: only 17 trades — too few to be calm about; it did not beat buy & hold in this window.",
        },
        "breadth": {
            "planned": 60,
            "ran": 60,
            "beat_buy_hold_pct": 0.0,
            "clean_pct": 0.0,
            "median_excess_pct": -166.35583860761096,
            "median_total_return_pct": 18.77555039076444,
            "median_sharpe": 0.5027187621411935,
            "median_max_drawdown_pct": -11.642615999525024,
            "total_trades": 4039,
            "best_cell": "BTC/USDT 4h  30/60 s4.0 t5.0",
            "best_excess_pct": -52.70881946829121,
        },
        "based_on": "status.json, explore_notes.json, robustness.json",
        "generated": "2026-10-16T10:00:00+00:00",
        "notes": [],
        "verification": {},
    }


def _seed(root: Path, payload: dict) -> None:
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)

    (results / "status.json").write_text(
        json.dumps(
            {
                "generated": "2026-10-16T10:00:00Z",
                "paper": {
                    "latest_active": payload["paper"]["file"],
                    "latest_metrics": payload["paper"],
                },
            }
        )
    )
    (results / "explore_notes.json").write_text(
        json.dumps(
            {
                "breadth_read": {
                    "planned": payload["breadth"]["planned"],
                    "ran": payload["breadth"]["ran"],
                    "median_excess_pct": payload["breadth"]["median_excess_pct"],
                }
            }
        )
    )
    (results / "robustness.json").write_text(
        json.dumps(
            {
                "summary": {
                    "ran": payload["breadth"]["ran"],
                    "median_excess_pct": payload["breadth"]["median_excess_pct"],
                    "best": {"label": payload["breadth"]["best_cell"]},
                    "planned": payload["breadth"]["planned"],
                },
                "nulls": [],
            }
        )
    )
    (results / "dashboard_excess.json").write_text(json.dumps(payload))


def _open(page, host: str, port: int) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.set_viewport_size(PHONE)
    page.goto(URL.format(port=port), wait_until="domcontentloaded", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(600)
    return errors


def _read_panel(page) -> dict:
    return page.evaluate(
        """() => {
            const ids = [
                "excessBasedOn",
                "excessTitle",
                "excessSub",
                "excessExcess",
                "excessBench",
                "excessTotalReturn",
                "excessSharpe",
                "excessMaxDD",
                "excessBarsInMarket",
                "excessFees",
                "excessRead",
                "breadthPlanned",
                "breadthBeat",
                "breadthClean",
                "breadthMedianExcess",
                "breadthMedianReturn",
                "breadthMedianSharpe",
                "breadthMedianDD",
            ];
            const out = {};
            for (const id of ids) {
                const el = document.getElementById(id);
                out[id] = el ? el.textContent : null;
            }
            return out;
        }
        """
    )


def _open_excess_dialog(page) -> None:
    """Open the Excess & breadth dialog and wait for it to be open.

    The live dashboard on a narrow window opens the dialog only in response to a
    user gesture, so we briefly poll ``dialog[open]`` after the click rather than
    waiting on element visibility (a ``<dialog>`` is part of the document before it
    is shown).
    """
    expect(page.locator("#openExcess")).to_be_visible(timeout=5000)
    page.locator("#openExcess").click()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if page.evaluate("""() => !!document.querySelector('#excessPanel[open]')"""):
            return
        time.sleep(0.1)
    raise AssertionError("Excess & breadth dialog did not open within 8s")


def test_excess_panel_renders_from_dashboard_excess_json() -> None:
    """The Excess & breadth dialog renders the paper and breadth fields from
    results/dashboard_excess.json without console errors.

    The panel sits in the DOM in a closed state until the user opens it, so the
    test opens it explicitly and then reads the rendered fields.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        payload = _payload()
        _seed(tmp_dir, payload)

        httpd, (host, port) = _run_server(tmp_dir, "127.0.0.1", 0)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    errors = _open(page, host, port)

                    _open_excess_dialog(page)
                    page.wait_for_timeout(400)

                    panel = _read_panel(page)

                    assert panel["excessBasedOn"] == payload["based_on"], panel
                    assert payload["paper"]["symbol"] in panel["excessTitle"], panel
                    assert panel["excessExcess"] == "-161.59%", panel
                    assert panel["excessBench"] == "180.13%", panel
                    assert panel["excessTotalReturn"] == "+18.53%", panel
                    assert panel["excessSharpe"] == "0.60", panel
                    assert panel["excessMaxDD"] == "-7.16%", panel
                    assert panel["excessBarsInMarket"] == "+17.07%", panel
                    assert panel["excessFees"] == "+0.96%", panel
                    assert "17 trades" in panel["excessSub"], panel
                    assert panel["breadthPlanned"] == "60 planned · 60 ran", panel
                    assert panel["breadthBeat"] == "-0.00%", panel
                    assert panel["breadthClean"] == "-0.00%", panel
                    assert panel["breadthMedianExcess"] == "-166.36%", panel
                    assert panel["breadthMedianReturn"] == "+18.78%", panel
                    assert panel["breadthMedianSharpe"] == "0.50", panel
                    assert panel["breadthMedianDD"] == "-11.64%", panel

                    # The panel toggles its read badge on breadth underperformance.
                    expect(page.locator("#excessRead")).to_contain_class(
                        "warn", timeout=3000
                    )
                finally:
                    browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    assert not errors, errors


def test_excess_panel_stays_closed_when_sources_are_missing() -> None:
    """A freshly-built results tree has no dashboard_excess.json yet; opening
    the panel opens it in its closed placeholder state and does not inject
    the paper/breadth fields from a snapshot that does not exist.

    On the mobile viewport the panel also has no auto-open step: the button
    fully controls that lifecycle.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        results = tmp_dir / "results"
        results.mkdir(parents=True, exist_ok=True)

        # Two of three backing sources present: the dashboard still refuses to
        # serve a stale snapshot when it is missing, and the panel is not magic.
        (results / "status.json").write_text(
            json.dumps({"generated": "2026-10-16T10:00:00Z"})
        )
        (results / "explore_notes.json").write_text(
            json.dumps({"breadth_read": {"planned": 60, "ran": 60}})
        )

        httpd, (host, port) = _run_server(tmp_dir, "127.0.0.1", 0)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    errors = _open(page, host, port)

                    _open_excess_dialog(page)
                    page.wait_for_timeout(600)

                    seen = _read_panel(page)

                    # Everything that depends on the frozen snapshot is blank.
                    for name in (
                        "excessBasedOn",
                        "excessTitle",
                        "excessSub",
                        "excessExcess",
                        "excessBench",
                        "breadthPlanned",
                        "breadthMedianExcess",
                    ):
                        assert seen.get(name) in (None, ""), (
                            name,
                            seen,
                        )

                    assert (
                        page.evaluate("""() => window.__attemptedExcessLoad""") is False
                    )

                    expect(page.locator("#excessRead")).not_to_contain_class(
                        "warn", timeout=3000
                    )

                    # The guarded 404 for the missing snapshot is the scenario
                    # under test: Chromium always logs its own
                    # "Failed to load resource" error for it, so only anything
                    # beyond that message is a real regression.
                    unexpected = [
                        e for e in errors
                        if not ("Failed to load resource" in e and "404" in e)
                    ]
                    assert not unexpected, unexpected
                finally:
                    browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    import sys

    sys.exit(__import__("pytest").main(
        [__file__, "-q", "-k", "test_excess_panel_renders_from_dashboard_excess_json or test_excess_panel_stays_closed_when_sources_are_missing", *sys.argv[1:]]
    ))
