"""First-time login helper for the Playwright MCP server.

Launches Chromium with the same persistent profile the MCP server uses,
opens https://x.com/home, and waits for you to finish logging in. Once
the page settles on /home with an `auth_token` cookie, it exits.

Usage:
    python login.py

Env:
    TWITTER_PROXY    e.g. http://127.0.0.1:7897
    TWITTER_PROFILE  override user-data-dir (default ~/.twitter-dm-mcp/browser-data)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

DEFAULT_PROFILE_DIR = Path.home() / ".twitter-dm-mcp" / "browser-data"
X_BASE = "https://x.com"
LOGIN_URL_MARKERS = ("/login", "/i/flow/login")


async def has_auth_cookie(ctx: BrowserContext) -> bool:
    cookies = await ctx.cookies(X_BASE)
    return any(c.get("name") == "auth_token" and c.get("value") for c in cookies)


def on_login_page(url: str) -> bool:
    return any(marker in url for marker in LOGIN_URL_MARKERS)


async def main() -> int:
    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)

    proxy_url = os.environ.get("TWITTER_PROXY") or None
    proxy = {"server": proxy_url} if proxy_url else None

    print("=== Twitter DM MCP - first-time login ===")
    print(f"Profile dir: {profile_dir}")
    if proxy_url:
        print(f"Proxy: {proxy_url}")
    else:
        print("Proxy: (none)  -- set TWITTER_PROXY if x.com is blocked on your network")
    print("A Chromium window will open. Log in to X normally;")
    print("this script auto-detects login completion and exits.")
    print()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            proxy=proxy,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page: Page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        exit_code = 0
        try:
            try:
                await page.goto(f"{X_BASE}/home", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                msg = str(e).splitlines()[0]
                print(f"Couldn't reach {X_BASE}/home: {msg}")
                if not proxy_url:
                    print("Hint: x.com is likely blocked on your network. Set TWITTER_PROXY and retry:")
                    print('  $env:TWITTER_PROXY = "http://127.0.0.1:7897"')
                    print("  python login.py")
                else:
                    print(f"The proxy {proxy_url} doesn't seem to be working. Check it and retry.")
                print("Aborting - cannot verify login without network access to x.com.")
                return 1

            if await has_auth_cookie(ctx) and not on_login_page(page.url):
                print("Already logged in (verified via /home).")
                print(f"Profile saved at: {profile_dir}")
                print("You can now run server.py with HEADLESS=1.")
                return 0

            if await has_auth_cookie(ctx):
                print("Found a stale auth_token cookie - x.com bounced us to login.")
                print("Please log in again in the browser window.")
            else:
                print("Not logged in. Please complete login in the browser window.")
            print("Waiting for login... (Ctrl+C to abort)")

            try:
                while True:
                    if not ctx.pages:
                        print("All pages closed before login completed. Aborting.")
                        exit_code = 1
                        break
                    if await has_auth_cookie(ctx) and not on_login_page(page.url):
                        print("Login detected.")
                        print(f"Profile saved at: {profile_dir}")
                        print("You can now run server.py with HEADLESS=1.")
                        break
                    await asyncio.sleep(2)
            except KeyboardInterrupt:
                print("\nAborted by user.")
                exit_code = 130
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
        return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
