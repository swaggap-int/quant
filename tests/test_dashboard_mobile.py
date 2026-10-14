"""Smoke test the dashboard at a mobile viewport.

Prerequisites: playwright + chromium installed. Server must already be running.

Checks:
- page loads
- no console errors
- chart canvas is visible
- a shortcut hint is present (so keyboard support is discoverable on mobile too)
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright, expect

URL = "http://127.0.0.1:8765/index.html"


def test_dashboard_renders_at_mobile_viewport() -> None:
    """The dashboard still works at a typical phone width."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(600)

            page.set_viewport_size({"width": 428, "height": 926})
            page.wait_for_timeout(300)

            title = page.locator("#title")
            expect(title).to_be_visible(timeout=5000)
            page_title = title.text_content()
            assert page_title, page_title

            canvas = page.locator("#equityCanvas")
            expect(canvas).to_be_visible(timeout=5000)

            shortcuts = page.locator("#shortcuts")
            assert shortcuts.count() > 0
            assert shortcuts.is_visible()

            has_touch = page.evaluate("typeof markHover === 'function'")
            assert has_touch, has_touch
        finally:
            browser.close()

    assert not errors, errors
