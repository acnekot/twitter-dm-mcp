"""Incremental DM checker: emit JSON of only new / updated items.

Scrapes the most recent N items from three sources:
  - chat:      /i/chat (accepted conversations)
  - priority:  /i/chat/requests (Priority tab — X-classified real requests)
  - hidden:    /i/chat/requests (Hidden tab — typically spam / cold outreach)

Diffs against a local state file (default ~/.twitter-dm-mcp/dm_state.json).
For each source emits:
    new:     conv_ids not seen before
    updated: conv_ids whose last_preview text changed
    unchanged: count only

After the run the state file is overwritten with the current snapshot so
next invocation only reports what's truly new.

Designed for feeding an LLM. The shape is stable:

  {
    "fetched_at": "<ISO8601>",
    "self_user_id": "<id|null>",
    "is_first_run": <bool>,
    "sources": {
      "chat":     {"new": [...], "updated": [...], "unchanged_count": N},
      "priority": {"new": [...], "updated": [...], "unchanged_count": N},
      "hidden":   {"new": [...], "updated": [...], "unchanged_count": N}
    }
  }

Each item:
  {conversation_id, other_user_id, name, time_relative, last_preview,
   avatar, change: "new"|"updated", previous_preview: "<str>|null"}

Usage:
    python dm_check.py                                # default: 50 each
    python dm_check.py --max 100
    python dm_check.py --sources chat,priority        # skip hidden
    python dm_check.py --out check.json
    python dm_check.py --state ./dm_state.json
    python dm_check.py --reset-state                  # force first-run mode
    python dm_check.py --headed                       # show browser

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
    _extract_inbox_dom,
    _is_logged_in,
    _self_user_id_from_cookies,
)

DEFAULT_STATE_PATH = DEFAULT_PROFILE_DIR.parent / "dm_state.json"
STATE_VERSION = 1
SOURCES = ("chat", "priority", "hidden")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "sources": {s: {} for s in SOURCES}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"warn: state file {path} unreadable ({e}); treating as fresh", file=sys.stderr)
        return {"version": STATE_VERSION, "sources": {s: {} for s in SOURCES}}
    # Ensure expected keys
    data.setdefault("sources", {})
    for s in SOURCES:
        data["sources"].setdefault(s, {})
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def diff_items(
    items: list[dict], prev: dict[str, str]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compare current items against previous {conv_id: last_preview} map.
    Returns (new, updated, unchanged) — each as a list of items with
    a `change` field set.
    """
    new_items: list[dict] = []
    updated: list[dict] = []
    unchanged: list[dict] = []
    for it in items:
        cid = it.get("conversation_id")
        if not cid:
            continue
        cur_preview = it.get("last_preview") or ""
        if cid not in prev:
            new_items.append({**it, "change": "new", "previous_preview": None})
        elif prev[cid] != cur_preview:
            updated.append({
                **it,
                "change": "updated",
                "previous_preview": prev[cid],
            })
        else:
            unchanged.append({**it, "change": "unchanged", "previous_preview": None})
    return new_items, updated, unchanged


async def gather_chat(page, max_count: int) -> list[dict]:
    """Top of the main inbox -- accepted conversations."""
    try:
        await page.goto(f"{X_BASE}{X_INBOX_PATH}", wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"goto inbox failed: {e}", file=sys.stderr)
        return []
    try:
        await page.wait_for_selector(_INBOX_ITEM_SELECTOR, timeout=20000)
    except Exception:
        print("inbox items didn't render", file=sys.stderr)
        return []
    # Inbox top is sorted newest-first; no need to scroll for "latest 50".
    return await _extract_inbox_dom(page, max_count)


async def gather_requests(page, tab: str, max_count: int, max_wait_s: float) -> list[dict]:
    """One of priority / hidden tabs under /i/chat/requests."""
    if X_REQUESTS_PATH not in page.url:
        try:
            await page.goto(f"{X_BASE}{X_REQUESTS_PATH}", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"goto requests failed: {e}", file=sys.stderr)
            return []
        await asyncio.sleep(1)

    selector = _REQUEST_TAB_HIDDEN if tab == "hidden" else _REQUEST_TAB_PRIORITY
    try:
        await page.click(selector, timeout=8000)
    except Exception:
        # Priority is the default landing tab; ignore if click failed there.
        if tab == "hidden":
            print(f"warn: couldn't click Hidden tab", file=sys.stderr)
            return []
    await asyncio.sleep(1)
    try:
        await page.wait_for_selector(_REQUEST_ITEM_SELECTOR, timeout=15000)
    except Exception:
        return []
    return await _collect_request_items(page, max_count, max_wait_s=max_wait_s)


async def main(opts: argparse.Namespace) -> int:
    sources = [s.strip() for s in opts.sources.split(",") if s.strip()]
    for s in sources:
        if s not in SOURCES:
            print(f"unknown source: {s}", file=sys.stderr)
            return 64

    state_path = Path(opts.state).expanduser()
    if opts.reset_state and state_path.exists():
        state_path.unlink()
        print(f"deleted state file {state_path}", file=sys.stderr)
    state = load_state(state_path)
    is_first_run = all(not state["sources"].get(s) for s in sources)

    profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
    headless_env = os.environ.get("HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    headless = False if opts.headed else headless_env
    proxy_url = os.environ.get("TWITTER_PROXY") or None
    proxy = {"server": proxy_url} if proxy_url else None

    print(
        f"profile={profile_dir} headless={headless} proxy={proxy_url} "
        f"max={opts.max} sources={sources} state={state_path} first_run={is_first_run}",
        file=sys.stderr,
    )

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

            self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))

            # Always warm the SPA via /i/chat first; the requests routes
            # depend on the XChat bundle being hot.
            results: dict[str, list[dict]] = {}
            if "chat" in sources:
                print("gather chat...", file=sys.stderr)
                results["chat"] = await gather_chat(page, opts.max)
                print(f"  got {len(results['chat'])}", file=sys.stderr)
            else:
                # Warm the SPA anyway
                try:
                    await page.goto(f"{X_BASE}{X_INBOX_PATH}", wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_selector(_INBOX_ITEM_SELECTOR, timeout=15000)
                except Exception:
                    pass

            if "priority" in sources:
                print("gather priority...", file=sys.stderr)
                results["priority"] = await gather_requests(page, "priority", opts.max, max_wait_s=30.0)
                print(f"  got {len(results['priority'])}", file=sys.stderr)

            if "hidden" in sources:
                print("gather hidden...", file=sys.stderr)
                results["hidden"] = await gather_requests(page, "hidden", opts.max, max_wait_s=60.0)
                print(f"  got {len(results['hidden'])}", file=sys.stderr)

            out: dict = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "self_user_id": self_id,
                "is_first_run": is_first_run,
                "sources": {},
            }

            new_state_sources = dict(state["sources"])
            for src, items in results.items():
                prev_map = state["sources"].get(src) or {}
                new_items, updated, unchanged_items = diff_items(items, prev_map)
                out["sources"][src] = {
                    "new": new_items,
                    "updated": updated,
                    "unchanged": unchanged_items,
                    "unchanged_count": len(unchanged_items),
                    "total_scanned": len(items),
                }
                # Update state for this source -- only update conv_ids we saw
                # this run; leave older entries intact so a temporarily-missing
                # conv_id doesn't get falsely flagged as "new" next time.
                merged = dict(prev_map)
                for it in items:
                    cid = it.get("conversation_id")
                    if cid:
                        merged[cid] = it.get("last_preview") or ""
                new_state_sources[src] = merged

            out_path = Path(opts.out)
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

            new_state = {
                "version": STATE_VERSION,
                "updated_at": out["fetched_at"],
                "sources": new_state_sources,
            }
            save_state(state_path, new_state)

            summary = " ".join(
                f"{src}=+{len(v['new'])}/~{len(v['updated'])}/={v['unchanged_count']}"
                for src, v in out["sources"].items()
            )
            print(
                f"wrote {out_path} ({out_path.stat().st_size:,} bytes) | {summary}",
                file=sys.stderr,
            )
            return 0
        finally:
            await ctx.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Incremental DM check -> JSON for AI summarization.")
    p.add_argument("--out", default="dm_check.json", help="output JSON path")
    p.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="state file path")
    p.add_argument("--max", type=int, default=50, help="latest N items per source")
    p.add_argument(
        "--sources",
        default="chat,priority,hidden",
        help="comma-separated subset of: chat,priority,hidden",
    )
    p.add_argument("--reset-state", action="store_true", help="delete state file first")
    p.add_argument("--headed", action="store_true", help="show browser")
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
