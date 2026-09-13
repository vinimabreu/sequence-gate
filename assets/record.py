"""Frame recorder: one browser, seek per frame, screenshot per frame.

Launching a browser per frame takes about fifteen minutes for a short clip. Opening one
page and calling `window.__seek(ms)` takes about two.

    python assets/record.py assets/anim.html assets/frames --fps 25 --dur 21000
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--dur", type=int, default=21000, help="milliseconds")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    total = int(args.dur / 1000 * args.fps)
    step = args.dur / total
    url = Path(args.html).resolve().as_uri()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.goto(url)
        page.wait_for_function("typeof window.__seek === 'function'")
        for i in range(total):
            page.evaluate(f"window.__seek({i * step})")
            page.screenshot(path=str(out / f"f{i:05d}.png"))
            if i % 50 == 0:
                print(f"  {i}/{total}")
        browser.close()

    print(f"{total} frames in {out}")


if __name__ == "__main__":
    main()
