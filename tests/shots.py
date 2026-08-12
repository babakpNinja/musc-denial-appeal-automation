#!/usr/bin/env python3
"""Screenshot the dashboard at phone / tablet / desktop widths.

    python tests/shots.py [base_url] [out_prefix]

Also exercises the status workflow through the UI (opens a case, submits it)
so the shots show real chips, timeline and the mobile modal.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8123"
PREFIX = Path(sys.argv[2] if len(sys.argv) > 2 else "_shot")
VIEWPORTS = {
    "phone": {"width": 390, "height": 844, "mobile": True},     # iPhone 14
    "tablet": {"width": 820, "height": 1180, "mobile": True},   # iPad Air
    "desktop": {"width": 1440, "height": 900, "mobile": False},
}


def shoot(page, name: str):
    out = f"{PREFIX}-{name}.png"
    page.screenshot(path=out, full_page=name.endswith("-full"))
    print("wrote", out)


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, vp in VIEWPORTS.items():
            ctx = browser.new_context(viewport={"width": vp["width"], "height": vp["height"]},
                                      device_scale_factor=2, is_mobile=vp["mobile"],
                                      has_touch=vp["mobile"])
            page = ctx.new_page()
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector("#caseRows tr")
            shoot(page, name)
            shoot(page, f"{name}-full")

            # open the top case and show the workflow panel
            page.click("#caseRows tr:first-child")
            page.wait_for_selector("#wfPanel")
            page.wait_for_timeout(700)
            shoot(page, f"{name}-case")
            ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
