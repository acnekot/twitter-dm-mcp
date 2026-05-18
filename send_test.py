"""Test the XChat composer: open a conversation, type text, optionally send.

Usage:
    python send_test.py <conv_id> "<text>"            # DRY-RUN (no Enter)
    python send_test.py <conv_id> "<text>" --send     # actually send
    python send_test.py <conv_id> "<text>" --headed   # show browser
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
    _COMPOSER_TEXTAREA_SELECTOR,
    _MESSAGE_LIST_SELECTOR,
    _is_logged_in,
    _open_conversation,
    _send_in_active_conversation,
)


async def main(conv_id: str, text: str, do_send: bool, headed: bool) -> int:
    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    headless_env = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    headless = False if headed else headless_env
    proxy_url = os.environ.get("TWITTER_PROXY") or None
    proxy = {"server": proxy_url} if proxy_url else None

    print(f"profile={profile_dir}  headless={headless}  proxy={proxy_url}", file=sys.stderr)
    print(f"conv_id={conv_id}  send={do_send}  text={text!r}", file=sys.stderr)

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

            try:
                await page.goto(f"{X_BASE}{X_INBOX_PATH}", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"goto inbox failed: {e}", file=sys.stderr)

            await _open_conversation(page, conv_id)

            try:
                await page.wait_for_selector(_MESSAGE_LIST_SELECTOR, timeout=20000)
            except Exception:
                print("Conversation didn't load.", file=sys.stderr)
                return 2

            # Verify the composer textarea exists before we touch anything.
            try:
                await page.wait_for_selector(
                    _COMPOSER_TEXTAREA_SELECTOR, timeout=10000, state="visible"
                )
            except Exception:
                print(f"Composer textarea {_COMPOSER_TEXTAREA_SELECTOR} not visible.", file=sys.stderr)
                print(
                    "If this conversation is a pending message request, you may "
                    "need to accept it first.",
                    file=sys.stderr,
                )
                return 3

            status = await _send_in_active_conversation(page, text, dry_run=not do_send)
            # Verify the textarea actually received the text (works for either mode)
            content = await page.evaluate(
                """sel => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    return (el.value !== undefined) ? el.value : (el.innerText || '');
                }""",
                _COMPOSER_TEXTAREA_SELECTOR,
            )
            status["composer_content_after"] = content
            print(json.dumps(status, ensure_ascii=False, indent=2))

            if not do_send:
                # Hold so we can visually confirm the text is in the composer.
                print("[dry-run] text inserted into composer; sleeping 4s before exit", file=sys.stderr)
                await page.screenshot(path="send_dryrun.png")
                await asyncio.sleep(4)
            else:
                # Give some time for the message to appear in the list.
                await asyncio.sleep(3)
                await page.screenshot(path="send_after.png")

            return 0
        finally:
            await ctx.close()


def parse_args(argv: list[str]) -> tuple[str, str, bool, bool]:
    headed = False
    do_send = False
    positional: list[str] = []
    for a in argv:
        if a == "--headed":
            headed = True
        elif a == "--send":
            do_send = True
        else:
            positional.append(a)
    if len(positional) < 2:
        print('usage: python send_test.py <conv_id> "<text>" [--send] [--headed]', file=sys.stderr)
        sys.exit(64)
    return positional[0], positional[1], do_send, headed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    conv_id, text, do_send, headed = parse_args(sys.argv[1:])
    try:
        sys.exit(asyncio.run(main(conv_id, text, do_send, headed)))
    except KeyboardInterrupt:
        sys.exit(130)
