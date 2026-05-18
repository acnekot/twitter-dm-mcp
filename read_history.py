"""Standalone DM history reader (no MCP).

Usage:
    python read_history.py <conv_id>            # e.g. "1441009715782115342:1552213521253150720"
    python read_history.py <conv_id> 30         # max 30 messages
    python read_history.py <conv_id> --headed
    python read_history.py <conv_id> --raw

Env: same as server.py.
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
    _MESSAGE_LIST_SELECTOR,
    _MESSAGE_TEXT_SELECTOR,
    _extract_conversation_dom,
    _is_logged_in,
    _open_conversation,
    _self_user_id_from_cookies,
    _wait_for_messages_stable,
)


async def main(conv_id: str, count: int, raw: bool, headed: bool) -> int:
    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    headless_env = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    headless = False if headed else headless_env
    proxy_url = os.environ.get("TWITTER_PROXY") or None
    proxy = {"server": proxy_url} if proxy_url else None

    print(f"profile={profile_dir}  headless={headless}  proxy={proxy_url}  conv_id={conv_id}", file=sys.stderr)

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

            # Boot the SPA via /i/chat first so in-app navigation works.
            try:
                await page.goto(f"{X_BASE}{X_INBOX_PATH}", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"goto inbox failed: {e}", file=sys.stderr)

            await _open_conversation(page, conv_id)

            try:
                await page.wait_for_selector(_MESSAGE_LIST_SELECTOR, timeout=25000)
            except Exception:
                print("Message list didn't render within 25s.", file=sys.stderr)
                return 2
            stable_count = await _wait_for_messages_stable(
                page, min_messages=1, settle_ms=1500, timeout_s=15.0
            )
            print(f"messages settled at count={stable_count}", file=sys.stderr)

            msgs = await _extract_conversation_dom(page, count)

            self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))
            other_id = None
            parts = conv_id.split(":")
            if len(parts) == 2 and self_id:
                other_id = next((p for p in parts if p != self_id), None)

            out = {"conversation_id": conv_id, "other_user_id": other_id, "messages": msgs}

            if raw:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                print(f"Conversation {conv_id}  (other={other_id})", file=sys.stderr)
                print(f"Got {len(msgs)} messages:\n")
                for m in msgs:
                    mine = m.get("mine")
                    if mine is True:
                        who = "me"
                    elif mine is False:
                        who = "them"
                    else:
                        who = "?"
                    kind = m.get("kind") or "text"
                    text = (m.get("text") or "").replace("\n", " ")
                    if kind == "media" and not text:
                        text = "[media/attachment]"
                    if len(text) > 200:
                        text = text[:197] + "..."
                    print(f"  [{who:>4}] {text}")
            return 0
        finally:
            await ctx.close()


def parse_args(argv: list[str]) -> tuple[str, int, bool, bool]:
    raw = False
    headed = False
    count = 50
    conv_id = ""
    for a in argv:
        if a == "--raw":
            raw = True
        elif a == "--headed":
            headed = True
        elif a.isdigit():
            count = int(a)
        elif ":" in a and not conv_id:
            conv_id = a
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            sys.exit(64)
    if not conv_id:
        print("usage: python read_history.py <conv_id> [count] [--raw] [--headed]", file=sys.stderr)
        sys.exit(64)
    return conv_id, count, raw, headed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    conv_id, count, raw, headed = parse_args(sys.argv[1:])
    try:
        sys.exit(asyncio.run(main(conv_id, count, raw, headed)))
    except KeyboardInterrupt:
        sys.exit(130)
