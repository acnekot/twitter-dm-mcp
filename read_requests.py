"""Standalone reader for /i/chat/requests (message requests / pending DMs).

Two tabs:
  priority : X's classifier surfaces "real" requests here
  hidden   : everything else (typically spam / fan messages / cold outreach)

Output is compact JSON or a human listing -- pipe --raw into a model for
summarization.

Usage:
    python read_requests.py                        # priority, default 50
    python read_requests.py hidden 200             # hidden tab, scroll for up to 200
    python read_requests.py priority --raw         # JSON to stdout
    python read_requests.py hidden 500 --headed    # show the browser

Env (same as server.py):
    HEADLESS=0|1, TWITTER_PROXY, TWITTER_PROFILE
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
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
    _collect_request_items,
    _is_logged_in,
)


async def main(tab: str, max_count: int, raw: bool, headed: bool) -> int:
    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    headless_env = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    headless = False if headed else headless_env
    proxy_url = os.environ.get("TWITTER_PROXY") or None
    proxy = {"server": proxy_url} if proxy_url else None

    print(f"profile={profile_dir}  headless={headless}  proxy={proxy_url}  tab={tab}  max={max_count}", file=sys.stderr)

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
                print("Not logged in. Run `python login.py` first.", file=sys.stderr)
                return 1

            # Warm the SPA so /i/chat/requests routes correctly.
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

            if tab == "hidden":
                try:
                    await page.click(_REQUEST_TAB_HIDDEN, timeout=8000)
                except Exception:
                    print("Couldn't click Hidden tab.", file=sys.stderr)
                    return 2
                await asyncio.sleep(1)

            try:
                await page.wait_for_selector(_REQUEST_ITEM_SELECTOR, timeout=15000)
            except Exception:
                print("No request items rendered (empty tab?).", file=sys.stderr)
                items: list[dict] = []
            else:
                items = await _collect_request_items(page, max_count, max_wait_s=90.0)
                print(f"collected {len(items)} unique items", file=sys.stderr)

            out = {"tab": tab, "count": len(items), "items": items}

            if raw:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                print(f"Tab: {tab}  ({len(items)} items)\n")
                for it in items:
                    name = it.get("name") or "(no name)"
                    t = it.get("time_relative") or ""
                    preview = (it.get("last_preview") or "").replace("\n", " ")
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    other = it.get("other_user_id") or "?"
                    cid = it.get("conversation_id") or "?"
                    print(f"- {name}  [{t}]")
                    print(f"    other_user_id={other}  conv_id={cid}")
                    if preview:
                        print(f"    preview: {preview}")
            return 0
        finally:
            await ctx.close()


def parse_args(argv: list[str]) -> tuple[str, int, bool, bool]:
    raw = False
    headed = False
    tab = "priority"
    max_count = 50
    for a in argv:
        if a == "--raw":
            raw = True
        elif a == "--headed":
            headed = True
        elif a in ("priority", "hidden"):
            tab = a
        elif a.isdigit():
            max_count = int(a)
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            sys.exit(64)
    return tab, max_count, raw, headed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    tab, mc, raw, headed = parse_args(sys.argv[1:])
    try:
        sys.exit(asyncio.run(main(tab, mc, raw, headed)))
    except KeyboardInterrupt:
        sys.exit(130)
