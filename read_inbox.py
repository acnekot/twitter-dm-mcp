"""Standalone DM inbox reader (no MCP).

Runs the same DOM-based inbox extraction as server.py's
`list_dm_conversations` and prints the result.

Usage:
    python read_inbox.py                    # list conversations (default 20)
    python read_inbox.py 50                 # list up to 50 conversations
    python read_inbox.py --raw              # dump raw extracted JSON
    python read_inbox.py --headed           # show the browser

Env (same as server.py):
    HEADLESS=0|1     default 1; --headed overrides to 0
    TWITTER_PROXY    e.g. http://127.0.0.1:7897
    TWITTER_PROFILE  override user-data-dir
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
    _INBOX_ITEM_SELECTOR,
    _extract_inbox_dom,
    _is_logged_in,
)


async def main(max_count: int, raw: bool, headed: bool) -> int:
    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    headless_env = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    headless = False if headed else headless_env
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
                print("Not logged in. Run `python login.py` first.", file=sys.stderr)
                return 1

            try:
                await page.goto(
                    f"{X_BASE}{X_INBOX_PATH}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                print(f"goto {X_INBOX_PATH} failed: {e}", file=sys.stderr)

            try:
                await page.wait_for_selector(_INBOX_ITEM_SELECTOR, timeout=25000)
            except Exception:
                print(
                    "Inbox didn't render any conversation items within 25s. "
                    "Try --headed to see what the page is doing.",
                    file=sys.stderr,
                )
                return 2

            items = await _extract_inbox_dom(page, max_count)

            if raw:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            else:
                print(f"Got {len(items)} conversations:\n")
                for it in items:
                    name = it.get("name") or "(no name)"
                    t = it.get("time_relative") or ""
                    preview = (it.get("last_preview") or "").replace("\n", " ")
                    if len(preview) > 80:
                        preview = preview[:77] + "..."
                    other = it.get("other_user_id") or "?"
                    cid = it.get("conversation_id") or "?"
                    print(f"- {name}  [{t}]")
                    print(f"    other_user_id={other}  conv_id={cid}")
                    if preview:
                        print(f"    last: {preview}")
            return 0
        finally:
            await ctx.close()


def parse_args(argv: list[str]) -> tuple[int, bool, bool]:
    raw = False
    headed = False
    max_count = 20
    for a in argv:
        if a == "--raw":
            raw = True
        elif a == "--headed":
            headed = True
        elif a.isdigit():
            max_count = int(a)
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            sys.exit(64)
    return max_count, raw, headed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    mc, raw, headed = parse_args(sys.argv[1:])
    try:
        sys.exit(asyncio.run(main(mc, raw, headed)))
    except KeyboardInterrupt:
        sys.exit(130)
