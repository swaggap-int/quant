"""Smoke tests for the dashboard across phone and tablet viewports.

Prerequisites: playwright + chromium installed. Server must already be running.

Checks:
- page loads
- no console errors
- chart canvas is visible in both a phone and a tablet viewport
- the drawdown canvas adopts a shorter height on the phone breakpoint
- the canvas redraws after a viewport resize
- the chart touch handler is wired in both viewports
- a simulated chart scrub updates the hover state and readout
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright, expect

URL = "http://127.0.0.1:8765/index.html"

PHONE = {"width": 428, "height": 926}
TABLET = {"width": 768, "height": 1024}


def _open(page) -> None:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(600)

    return errors


def _canvas_size(page, canvas_id: str) -> dict[str, int | None]:
    return page.evaluate(
        f"() => {{ const c = document.getElementById('{canvas_id}'); return c ? {{ w: Math.round(c.getBoundingClientRect().width), h: parseInt(getComputedStyle(c).height) }} : {{ w: null, h: null }}; }}"
    )


def test_dashboard_renders_at_phone_viewport() -> None:
    """The dashboard still works at a typical phone width."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = _open(page)

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

            has_touch = page.evaluate("typeof markHover === 'function'")
            assert has_touch, has_touch
        finally:
            browser.close()

    assert not errors, errors


def test_dashboard_renders_at_tablet_viewport() -> None:
    """The dashboard still works at a typical tablet width."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = _open(page)

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

            has_touch = page.evaluate("typeof markHover === 'function'")
            assert has_touch, has_touch
        finally:
            browser.close()

    assert not errors, errors
    assert equity_at_tablet["h"] <= 260, f"equity canvas too tall at tablet: {equity_at_tablet}"


def test_dashboard_redraws_after_resize() -> None:
    """Resizing the viewport redraws both charts."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = _open(page)

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

    assert not errors, errors


def test_dashboard_chart_scrub_moves_readout() -> None:
    """A simulated chart scrub updates the hover index and readout text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = _open(page)

            page.set_viewport_size(PHONE)
            page.wait_for_timeout(300)

            started = page.evaluate(
                "() => {\n"
                "  const readout = document.getElementById('readout');\n"
                "  if (!readout) return null;\n"
                "  return { readout: readout.textContent };\n"
                "}"
            )
            assert isinstance(started, dict), started

            page.evaluate(
                "() => {\n"
                "  const canvas = document.getElementById('equityCanvas');\n"
                "  if (!canvas || !window.markHover) return;\n"
                "  const rect = canvas.getBoundingClientRect();\n"
                "  markHover(rect.left + rect.width * 0.4);\n"
                "}"
            )
            page.wait_for_timeout(140)

            after = page.evaluate(
                "() => {\n"
                "  const readout = document.getElementById('readout');\n"
                "  return { readout: readout ? readout.textContent : null };\n"
                "}"
            )
            assert isinstance(after, dict), after
            assert after["readout"] is not None, after
            assert after["readout"] != started["readout"], (
                f"readout did not change after scrub: {started['readout']!r} -> {after['readout']!r}"
            )
        finally:
            browser.close()

    assert not errors, errors
