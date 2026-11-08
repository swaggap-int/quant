"""Playwright smoke tests for the dashboard at phone and tablet viewports.

Each test stands up its own lightweight HTTP server in-process (using the same
handler wiring as the dashboard's own tests), waits for a route to answer, then
drives Playwright against that server. That makes these suites runnable in
headless CI without relying on an external loopback dashboard.

Checks:
- page loads from the self-contained server
- no console errors
- chart canvas is visible in both a phone and a tablet viewport
- the drawdown canvas adopts a shorter height on the phone breakpoint
- the canvas redraws after a viewport resize
- a simulated chart scrub updates the hover readout

The build wires mousemove/touchmove to window.markHover, so the scrub tests
rely on that handler being present.
"""
from __future__ import annotations

import json
import math
import socket
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from quant.dashboard.server import RunIndex, make_handler, RESULTS_ROOT

PHONE = {"width": 428, "height": 926}
TABLET = {"width": 768, "height": 1024}


def _wait_for_port(host: str, port: int, deadline: float) -> None:
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"server on {host}:{port} did not start accepting connections")


def _run_server(
    results_dir: Path, host: str, port: int
) -> tuple[ThreadingHTTPServer, tuple[str, int]]:
    """Start a dashboard handler on ``(host, port)`` and return ``(httpd, actual_addr)``.

    The bound socket can report back a different port when ``port == 0`` (auto),
    so the caller must use the returned ``actual_addr`` for connections, not the
    requested ``(host, port)``.
    """
    results = results_dir / "results"
    import quant.dashboard.server as srv

    srv.RESULTS_ROOT = results
    srv._EXCESS_SOURCES = (
        results / "status.json",
        results / "explore_notes.json",
        results / "robustness.json",
    )

    handler = make_handler(RunIndex(results))
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.timeout = 20
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    actual = httpd.server_address
    _wait_for_port(actual[0], actual[1], time.time() + 5.0)
    return httpd, actual


def _fetch_excess_payload() -> dict:
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



def _demo_run(run_id: str) -> dict:
    """A small synthetic run so the charts have something to render.

    The page only draws the equity/drawdown canvases once ``/api/runs``
    lists a run and ``/api/run/<id>`` serves its series, so the mobile
    seeds must include one; the excess snapshot alone leaves the empty
    state up and the canvases hidden.
    """
    n = 120
    day_ms = 86_400_000
    t0 = 1_640_995_200_000  # 2022-01-01T00:00:00Z
    ts = [t0 + i * day_ms for i in range(n)]
    equity = [round(10_000 * (1 + 0.0009 * i + 0.006 * math.sin(i / 9.0)), 2) for i in range(n)]
    benchmark = [round(10_000 * (1 + 0.0016 * i), 2) for i in range(n)]
    close = [round(40_000 * (1 + 0.0016 * i + 0.01 * math.sin(i / 7.0)), 2) for i in range(n)]
    peak = equity[0]
    drawdown_pct = []
    for eq in equity:
        peak = max(peak, eq)
        drawdown_pct.append(round((eq - peak) / peak * 100, 3))
    total_return_pct = round((equity[-1] / equity[0] - 1) * 100, 2)
    buy_hold_return_pct = round((benchmark[-1] / benchmark[0] - 1) * 100, 2)
    return {
        "run_id": run_id,
        "created_at": "2022-05-01T00:00:00+00:00",
        "strategy": {"name": "ema-atr-trend"},
        "data": {
            "symbol": "BTC/USDT",
            "timeframe": "1d",
            "bars": n,
            "start": "2022-01-01",
            "end": "2022-04-30",
            "provider": "synthetic",
        },
        "metrics": {
            "trades": 12,
            "total_return_pct": total_return_pct,
            "buy_hold_return_pct": buy_hold_return_pct,
            "excess_return_pct": round(total_return_pct - buy_hold_return_pct, 2),
            "cagr_pct": 9.5,
            "max_drawdown_pct": min(drawdown_pct),
            "max_drawdown_bars": 9,
            "days_in_drawdown_pct": 22.5,
            "annual_vol": 38.2,
            "sharpe": 0.62,
            "sortino": 0.81,
            "calmar": 1.1,
            "bars_in_market_pct": 61.7,
            "win_rate_pct": 58.3,
            "profit_factor": 1.4,
            "expectancy": 12.5,
            "avg_win": 88.0,
            "avg_loss": -52.0,
            "best_trade": 210.0,
            "worst_trade": -160.0,
            "avg_bars_held": 6.5,
            "avg_r_multiple": 0.4,
            "fees_paid": 41.0,
            "fees_pct_of_start": 0.41,
            "turnover_multiple": 2.2,
            "final_equity": equity[-1],
            "years": round(n / 365, 4),
        },
        "series": {
            "ts": ts,
            "close": close,
            "equity": equity,
            "benchmark": benchmark,
            "drawdown_pct": drawdown_pct,
        },
        "trades": [],
    }

def _seed_results(dir: Path, payload: dict) -> None:
    results = dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "status.json").write_text(
        json.dumps(
            {
                "paper": {
                    "latest_active": payload["paper"]["file"],
                    "latest_metrics": payload["paper"],
                }
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
    run_id = "TESTMOBILE_1d_demo_20260101T000000Z"
    (results / f"{run_id}.json").write_text(json.dumps(_demo_run(run_id)))
    (results / "dashboard_excess.json").write_text(json.dumps(payload))


def _open(page, host: str, port: int) -> list[str]:
    errors: list[str] = []
    page.on(
        "console", lambda m: errors.append(m.text) if m.type == "error" else None
    )

    url = f"http://{host}:{port}/index.html"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(600)

    return errors


def _canvas_size(page, canvas_id: str) -> dict[str, int | None]:
    return page.evaluate(
        f"""
        () => {{
            const c = document.getElementById('{canvas_id}');
            return c ? {{
                w: Math.round(c.getBoundingClientRect().width),
                h: parseInt(getComputedStyle(c).height)
            }} : {{ w: null, h: null }};
        }}
        """
    )


def test_dashboard_renders_at_phone_viewport() -> None:
    """The dashboard still works at a typical phone width."""
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        tmp_dir = Path(tmp)
        payload = _fetch_excess_payload()
        _seed_results(tmp_dir, payload)

        httpd, (host, port) = _run_server(tmp_dir, "127.0.0.1", 0)
        try:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = _open(page, host, port)

                page.set_viewport_size(PHONE)
                page.wait_for_timeout(300)

                title = page.locator("#title")
                expect(title).to_be_visible(timeout=5000)
                page_title = title.text_content()
                assert page_title, page_title

                equity = page.locator("#equityCanvas")
                expect(equity).to_be_visible(timeout=5000)

                dd = page.locator("#ddCanvas")
                expect(dd).to_be_visible(timeout=5000)
                dd_height = dd.evaluate("el => parseInt(getComputedStyle(el).height)")
                assert dd_height <= 120, f"ddCanvas too tall on phone: {dd_height}"

                shortcuts = page.locator("#shortcuts")
                assert shortcuts.count() > 0
                assert shortcuts.is_visible()

                # The build wires mousemove/touchmove to markHover, so it must be present.
                has_touch = page.evaluate("typeof markHover === 'function'")
                assert has_touch, "chart scrub handler (markHover) should be wired"
            finally:
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    assert not errors, errors


def test_dashboard_renders_at_tablet_viewport() -> None:
    """The dashboard still works at a typical tablet width."""
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        tmp_dir = Path(tmp)
        payload = _fetch_excess_payload()
        _seed_results(tmp_dir, payload)

        httpd, (host, port) = _run_server(tmp_dir, "127.0.0.1", 0)
        try:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = _open(page, host, port)

                page.set_viewport_size(TABLET)
                page.wait_for_timeout(300)

                title = page.locator("#title")
                expect(title).to_be_visible(timeout=5000)
                page_title = title.text_content()
                assert page_title, page_title

                equity = page.locator("#equityCanvas")
                expect(equity).to_be_visible(timeout=5000)
                equity_at_tablet = _canvas_size(page, "equityCanvas")

                dd = page.locator("#ddCanvas")
                expect(dd).to_be_visible(timeout=5000)
                dd_height = dd.evaluate("el => parseInt(getComputedStyle(el).height)")
                assert dd_height <= 120, f"ddCanvas too tall at tablet: {dd_height}"

                shortcuts = page.locator("#shortcuts")
                assert shortcuts.count() > 0
                assert shortcuts.is_visible()

                # The build wires mousemove/touchmove to markHover, so it must be present.
                has_touch = page.evaluate("typeof markHover === 'function'")
                assert has_touch, "chart scrub handler (markHover) should be wired"
            finally:
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    assert not errors, errors
    assert equity_at_tablet["h"] <= 260, (
        f"equity canvas too tall at tablet: {equity_at_tablet}"
    )


def test_dashboard_redraws_after_resize() -> None:
    """Resizing the viewport redraws both charts."""
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        tmp_dir = Path(tmp)
        _seed_results(tmp_dir, _fetch_excess_payload())

        httpd, (host, port) = _run_server(tmp_dir, "127.0.0.1", 0)
        try:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = _open(page, host, port)

                page.set_viewport_size(PHONE)
                page.wait_for_timeout(300)

                first_phone = _canvas_size(page, "equityCanvas")
                page.set_viewport_size(TABLET)
                page.wait_for_timeout(300)

                second_tablet = _canvas_size(page, "equityCanvas")

                assert second_tablet["w"] > first_phone["w"], (
                    f"canvas width did not grow after resize: {first_phone} -> {second_tablet}"
                )
            finally:
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    assert not errors, errors


def test_dashboard_chart_scrub_moves_readout() -> None:
    """A simulated chart scrub updates the hover index and readout text."""
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        tmp_dir = Path(tmp)
        payload = _fetch_excess_payload()
        _seed_results(tmp_dir, payload)

        httpd, (host, port) = _run_server(tmp_dir, "127.0.0.1", 0)
        try:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = _open(page, host, port)

                page.set_viewport_size(PHONE)
                page.wait_for_timeout(300)

                # Wait for the chart data to actually load so the scrub has something
                # to read. The page fetches /results/dashboard_excess.json
                # asynchronously; before that completes the readout is only the
                # placeholder hint. Wait until the chart state is actually populated
                # (state.data present) or until the console shows a route-level
                # failure, whichever comes first.
                max_wait = 12  # seconds; monotonic() is in seconds, not ms
                deadline = time.monotonic() + max_wait
                state_loaded = False
                while time.monotonic() < deadline:
                    info = page.evaluate(
                        """() => ({ hasData: !!state?.data, seriesLen: state?.data?.series?.equity?.length ?? 0 })"""
                    )
                    if info.get("hasData") and info.get("seriesLen", 0) > 0:
                        state_loaded = True
                        break
                    page.wait_for_timeout(100)

                if not state_loaded:
                    available = page.evaluate(
                        """() => ({hasState: typeof state !== 'undefined',
                                stateKeys: typeof state !== 'undefined' ? Object.keys(state) : [],
                            readout: (() => {
                                const el = document.getElementById('readout');
                                return el ? el.textContent : null;
                            })()
                        })
                        """
                    )
                    raise AssertionError(
                        f"chart data did not load within {max_wait}s; state={available!r}; errors={errors!r}"
                    )

                started = page.evaluate(
                    """() => {
                        const readout = document.getElementById('readout');
                        if (!readout) return null;
                        return {
                            readout: readout.textContent,
                            seriesLen: state?.data?.series?.equity?.length ?? 0
                        };
                    }
                    """
                )
                assert isinstance(started, dict), started
                assert started.get("seriesLen", 0) > 0, (
                    f"chart data did not load before scrub: started={started!r}"
                )

                page.evaluate(
                    """() => {
                        const canvas = document.getElementById('equityCanvas');
                        if (!canvas || !window.markHover) return;
                        const rect = canvas.getBoundingClientRect();
                        window.markHover(rect.left + rect.width * 0.4);
                    }
                    """
                )
                page.wait_for_timeout(180)

                after = page.evaluate(
                    """() => {
                        const readout = document.getElementById('readout');
                        return { readout: readout ? readout.textContent : null };
                    }
                    """
                )
                assert isinstance(after, dict), after
                assert after["readout"] is not None, after
                assert after["readout"] != started["readout"], (
                    f"readout did not change after scrub: {started['readout']!r} -> {after['readout']!r}"
                )
            finally:
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    assert not errors, errors
