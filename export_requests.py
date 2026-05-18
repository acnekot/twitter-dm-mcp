"""Export DM message requests as a compact JSON for AI summarization.

Pulls both tabs (priority + hidden), accumulates unique items, and writes
a single JSON file with everything an LLM needs to bucket / summarize the
backlog:

    {
      "fetched_at": "...",
      "self_user_id": "...",
      "priority": {"count": N, "items": [...]},
      "hidden":   {"count": N, "items": [...]}
    }

Each item:
    {
      conversation_id, other_user_id,
      name,            # truncated to "User" by X in the hidden tab
      time_relative,   # "3h" / "4w" / "9w" ...
      last_preview,    # may be truncated by X's UI with ...
      avatar
    }

Usage:
    python export_requests.py                         # priority + hidden (up to 800 hidden)
    python export_requests.py --hidden-max 200
    python export_requests.py --priority-only
    python export_requests.py --out requests.json --headed

Env: HEADLESS / TWITTER_PROXY / TWITTER_PROFILE (same as server.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from server import (  # noqa: E402
    DEFAULT_PROFILE_DIR,
    X_BASE,
    X_INBOX_PATH,
    X_REQUESTS_PATH,
    _INBOX_ITEM_SELECTOR,
    _REQUEST_ITEM_SELECTOR,
    _REQUEST_TAB_HIDDEN,
    _REQUEST_TAB_PRIORITY,
    _collect_request_items,
    _is_logged_in,
    _self_user_id_from_cookies,
)


async def _gather_tab(page, tab: str, max_count: int, max_wait_s: float) -> list[dict]:
    if tab == "hidden":
        try:
            await page.click(_REQUEST_TAB_HIDDEN, timeout=8000)
        except Exception:
            print("warn: couldn't click Hidden tab", file=sys.stderr)
            return []
    else:
        # Priority is the default tab on cold load; clicking explicitly
        # ensures we're on it after switching back from Hidden.
        try:
            await page.click(_REQUEST_TAB_PRIORITY, timeout=4000)
        except Exception:
            pass
    await asyncio.sleep(1)
    try:
        await page.wait_for_selector(_REQUEST_ITEM_SELECTOR, timeout=15000)
    except Exception:
        return []
    return await _collect_request_items(page, max_count, max_wait_s=max_wait_s)


async def main(opts: argparse.Namespace) -> int:
    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    headless_env = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    headless = False if opts.headed else headless_env
    proxy_url = os.environ.get("TWITTER_PROXY") or None
    proxy = {"server": proxy_url} if proxy_url else None

    print(f"profile={profile_dir}  headless={headless}  proxy={proxy_url}", file=sys.stderr)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            proxy=proxy,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)

        try:
            if not await _is_logged_in(page):
                print("Not logged in.", file=sys.stderr)
                return 1

            self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))

            try:
                await page.goto(f"{X_BASE}{X_INBOX_PATH}", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"goto inbox failed: {e}", file=sys.stderr)
            try:
                await page.wait_for_selector(_INBOX_ITEM_SELECTOR, timeout=15000)
            except Exception:
                pass

            try:
                await page.goto(f"{X_BASE}{X_REQUESTS_PATH}", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"goto requests failed: {e}", file=sys.stderr)
            await asyncio.sleep(2)

            print("fetching priority tab...", file=sys.stderr)
            priority = await _gather_tab(page, "priority", opts.priority_max, max_wait_s=30.0)
            print(f"  priority: {len(priority)}", file=sys.stderr)

            hidden: list[dict] = []
            if not opts.priority_only:
                print("fetching hidden tab...", file=sys.stderr)
                hidden = await _gather_tab(page, "hidden", opts.hidden_max, max_wait_s=opts.hidden_max_wait)
                print(f"  hidden:   {len(hidden)}", file=sys.stderr)

            out = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "self_user_id": self_id,
                "priority": {"count": len(priority), "items": priority},
                "hidden": {"count": len(hidden), "items": hidden},
            }

            out_path = Path(opts.out)
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes)", file=sys.stderr)
            return 0
        finally:
            await ctx.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export DM message requests for AI summarization.")
    p.add_argument("--out", default="requests.json", help="output file path")
    p.add_argument("--priority-max", type=int, default=200)
    p.add_argument("--hidden-max", type=int, default=800)
    p.add_argument("--hidden-max-wait", type=float, default=180.0,
                   help="seconds to spend scrolling the Hidden tab")
    p.add_argument("--priority-only", action="store_true",
                   help="skip the Hidden tab")
    p.add_argument("--headed", action="store_true",
                   help="show the browser (overrides HEADLESS=1)")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    opts = parse_args(sys.argv[1:])
    try:
        sys.exit(asyncio.run(main(opts)))
    except KeyboardInterrupt:
        sys.exit(130)
