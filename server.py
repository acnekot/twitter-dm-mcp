"""Twitter DM MCP Server (Playwright-based).

Drives a real Chromium via Playwright with a persistent profile, so login
state survives across runs. Reads DM data by intercepting Twitter's
internal API responses (no DOM scraping). Sending uses the on-page
composer — that's the one place we touch the DOM.

Env:
  HEADLESS         '0' to show the browser (required for first login), '1' to hide. Default '1'.
  TWITTER_PROXY    e.g. http://127.0.0.1:7897 — passed to Playwright proxy.
  TWITTER_PROFILE  override user-data-dir. Default ~/.twitter-dm-mcp/browser-data
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Response,
    TimeoutError as PWTimeout,
    async_playwright,
)

DEFAULT_PROFILE_DIR = Path.home() / ".twitter-dm-mcp" / "browser-data"
X_BASE = "https://x.com"
X_INBOX_PATH = "/i/chat"  # X moved DM inbox here from /messages

mcp = FastMCP("twitter-dm")

_pw: Playwright | None = None
_ctx: BrowserContext | None = None
_page: Page | None = None
_browser_lock = asyncio.Lock()


def _log(msg: str) -> None:
    print(f"[twitter-dm-mcp] {msg}", file=sys.stderr, flush=True)


def _err(msg: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False)


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str)


async def _ensure_browser() -> Page:
    """Lazy-init Playwright + persistent context. Returns the (single) page."""
    global _pw, _ctx, _page
    if _page is not None and not _page.is_closed():
        return _page
    async with _browser_lock:
        if _page is not None and not _page.is_closed():
            return _page

        profile_dir = Path(os.environ.get("TWITTER_PROFILE") or DEFAULT_PROFILE_DIR)
        profile_dir.mkdir(parents=True, exist_ok=True)

        headless_env = os.environ.get("HEADLESS", "1").strip().lower()
        headless = headless_env not in ("0", "false", "no")

        proxy_url = os.environ.get("TWITTER_PROXY") or None
        proxy = {"server": proxy_url} if proxy_url else None

        _log(f"Launching Chromium (headless={headless}, profile={profile_dir}, proxy={proxy_url})")
        _pw = await async_playwright().start()
        _ctx = await _pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            proxy=proxy,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Persistent context starts with one blank page; reuse it.
        if _ctx.pages:
            _page = _ctx.pages[0]
        else:
            _page = await _ctx.new_page()
        _page.set_default_timeout(20000)
        return _page


async def _shutdown() -> None:
    global _pw, _ctx, _page
    try:
        if _ctx is not None:
            await _ctx.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _ctx = None
    _pw = None
    _page = None


async def _is_logged_in(page: Page) -> bool:
    """Cheap check: presence of `auth_token` cookie."""
    try:
        cookies = await page.context.cookies(X_BASE)
        return any(c.get("name") == "auth_token" and c.get("value") for c in cookies)
    except Exception:
        return False


ResponseFilter = Callable[[Response], bool]


async def _capture_first(
    page: Page,
    predicate: ResponseFilter,
    *,
    timeout: float = 20.0,
    debug: bool = False,
) -> dict[str, Any]:
    """Install a temporary listener; resolve with first matching JSON response."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()

    async def _try_resolve(resp: Response) -> None:
        if fut.done():
            return
        try:
            if not predicate(resp):
                return
        except Exception:
            return
        if debug:
            _log(f"capture: matched {resp.request.method} {resp.status} {resp.url[:140]}")
        try:
            data = await resp.json()
        except Exception as e:
            if debug:
                _log(f"capture: resp.json() failed for {resp.url[:120]} -> {type(e).__name__}: {e}")
            # Fallback: try raw text + manual parse
            try:
                text = await resp.text()
                data = json.loads(text)
                if debug:
                    _log(f"capture: text() fallback succeeded ({len(text)} bytes)")
            except Exception as e2:
                if debug:
                    _log(f"capture: text() fallback also failed -> {type(e2).__name__}: {e2}")
                return
        if not fut.done():
            fut.set_result(data)

    def _on_response(resp: Response) -> None:
        asyncio.create_task(_try_resolve(resp))

    page.on("response", _on_response)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


def _is_inbox_response(resp: Response) -> bool:
    u = resp.url
    if resp.request.method != "GET":
        return False
    return (
        # New GraphQL inbox endpoint (as of 2025+ /i/chat UI)
        "GetInboxPageRequestQuery" in u
        # Legacy REST endpoints (kept as fallback)
        or "/i/api/1.1/dm/inbox_initial_state.json" in u
        or "/i/api/1.1/dm/user_updates.json" in u
        or "DMInboxTimeline" in u
    )


def _is_conversation_response(resp: Response) -> bool:
    u = resp.url
    if resp.request.method != "GET":
        return False
    return (
        # New GraphQL single-conversation endpoint
        "GetConversationPageQuery" in u
        # Legacy REST / GraphQL endpoints (kept as fallback)
        or "/i/api/1.1/dm/conversation/" in u
        or "DMConversationTimeline" in u
    )


def _is_user_by_screen_name(resp: Response) -> bool:
    return "UserByScreenName" in resp.url


def _is_viewer_response(resp: Response) -> bool:
    u = resp.url
    return (
        "/i/api/1.1/account/settings.json" in u
        or "/i/api/1.1/account/verify_credentials.json" in u
        or "Viewer" in u
    )


def _parse_inbox(payload: dict[str, Any], max_count: int) -> list[dict[str, Any]]:
    """LEGACY REST/GraphQL parser. Kept for reference; X's new XChat (KMP)
    UI stores inbox state in IndexedDB and no longer ships a full inbox
    response over the network, so list_dm_conversations now scrapes DOM
    via `_extract_inbox_dom` instead.

    Structure (best-effort, defensive):
      payload['inbox_initial_state'] = {
        users: { uid: {id_str, name, screen_name, ...} },
        conversations: { conv_id: {participants: [{user_id}], sort_timestamp, ...} },
        entries: [{message: {message_data: {conversation_id, text, sender_id, time}}}, ...]
      }
    """
    state = payload.get("inbox_initial_state") or payload.get("user_events") or payload
    users = state.get("users", {}) or {}
    convs = state.get("conversations", {}) or {}
    entries = state.get("entries", []) or []

    last_msg_by_conv: dict[str, dict[str, Any]] = {}
    for e in entries:
        m = e.get("message") if isinstance(e, dict) else None
        if not m:
            continue
        md = m.get("message_data") or {}
        conv_id = md.get("conversation_id") or m.get("conversation_id")
        if not conv_id:
            continue
        prev = last_msg_by_conv.get(conv_id)
        ts = int(md.get("time", 0) or 0)
        if prev is None or ts > int(prev.get("time", 0) or 0):
            last_msg_by_conv[conv_id] = {
                "id": m.get("id"),
                "text": md.get("text"),
                "time": md.get("time"),
                "sender_id": md.get("sender_id"),
            }

    out: list[dict[str, Any]] = []
    sorted_convs = sorted(
        convs.items(),
        key=lambda kv: int((kv[1] or {}).get("sort_timestamp", 0) or 0),
        reverse=True,
    )
    for conv_id, c in sorted_convs[:max_count]:
        c = c or {}
        participants = []
        for p in c.get("participants", []) or []:
            uid = str(p.get("user_id") or "")
            u = users.get(uid) or {}
            participants.append({
                "id": uid,
                "name": u.get("name"),
                "screen_name": u.get("screen_name"),
            })
        out.append({
            "conversation_id": conv_id,
            "type": c.get("type"),
            "sort_timestamp": c.get("sort_timestamp"),
            "participants": participants,
            "last_message": last_msg_by_conv.get(conv_id),
        })
    return out


def _parse_conversation(payload: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """Pull messages from /i/api/1.1/dm/conversation/{id}.json."""
    conv = payload.get("conversation_timeline") or payload
    entries = conv.get("entries", []) or []
    msgs: list[dict[str, Any]] = []
    for e in entries:
        m = e.get("message") if isinstance(e, dict) else None
        if not m:
            continue
        md = m.get("message_data") or {}
        msgs.append({
            "id": m.get("id"),
            "text": md.get("text"),
            "time": md.get("time"),
            "sender_id": md.get("sender_id"),
            "recipient_id": md.get("recipient_id"),
            "conversation_id": md.get("conversation_id") or m.get("conversation_id"),
        })
    msgs.sort(key=lambda x: int(x.get("time") or 0), reverse=True)
    return msgs[:count]


def _self_user_id_from_cookies(cookies: list[dict[str, Any]]) -> str | None:
    """Extract own user_id from the `twid` cookie ('u%3D{id}' or 'u={id}')."""
    for c in cookies:
        if c.get("name") != "twid":
            continue
        v = c.get("value", "") or ""
        if "u%3D" in v:
            return v.split("u%3D", 1)[1]
        if "u=" in v:
            return v.split("u=", 1)[1]
    return None


_INBOX_ITEM_SELECTOR = '[data-testid^="dm-conversation-item-"]'
_REQUEST_ITEM_SELECTOR = '[data-testid^="dm-message-request-item-"]'
_REQUEST_TAB_PRIORITY = '[data-testid="dm-message-requests-tab-priority"]'
_REQUEST_TAB_HIDDEN = '[data-testid="dm-message-requests-tab-hidden"]'
_MESSAGE_LIST_SELECTOR = '[data-testid="dm-message-list"]'
_MESSAGE_TEXT_SELECTOR = '[data-testid^="message-text-"]'
_MESSAGE_BUBBLE_SELECTOR = '[data-testid^="message-"]:not([data-testid^="message-text-"])'
_COMPOSER_TEXTAREA_SELECTOR = '[data-testid="dm-composer-textarea"]'
_COMPOSER_FORM_SELECTOR = '[data-testid="dm-composer-form"]'
X_REQUESTS_PATH = "/i/chat/requests"


async def _wait_for_messages_stable(
    page: Page, min_messages: int = 1, settle_ms: int = 1500, timeout_s: float = 15.0
) -> int:
    """Wait until the message list stops growing (or shrinking) for `settle_ms`,
    then return the count. Returns 0 if no messages appeared within timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_count = -1
    last_change_ts = asyncio.get_event_loop().time()
    while True:
        now = asyncio.get_event_loop().time()
        count = await page.evaluate(
            f"""() => document.querySelectorAll('{_MESSAGE_BUBBLE_SELECTOR}').length"""
        )
        if count != last_count:
            last_count = count
            last_change_ts = now
        if count >= min_messages and (now - last_change_ts) * 1000 >= settle_ms:
            return count
        if now >= deadline:
            return last_count if last_count > 0 else 0
        await asyncio.sleep(0.3)


async def _extract_dm_items(
    page: Page,
    *,
    testid_prefix: str,
    max_count: int,
) -> list[dict[str, Any]]:
    """Generic scraper for any inbox-style list row.

    Used for both the main inbox (`dm-conversation-item-`) and the
    message requests list (`dm-message-request-item-`). Both render the
    same testid format `{prefix}{userA}:{userB}` and the same three-line
    innerText: name / relative time / last preview.
    """
    self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))

    raw = await page.evaluate(
        """([prefix, maxCount]) => {
            const out = [];
            const nodes = document.querySelectorAll(`[data-testid^="${prefix}"]`);
            for (const el of nodes) {
                const testid = el.getAttribute('data-testid') || '';
                const convId = testid.slice(prefix.length);
                const raw = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                const name = raw[0] || null;
                const timeRel = raw[1] || null;
                const preview = raw.length > 2 ? raw.slice(2).join(' ') : null;
                const img = el.querySelector('img');
                const avatar = img ? img.src : null;
                out.push({
                    conversation_id: convId,
                    name: name,
                    time_relative: timeRel,
                    last_preview: preview,
                    avatar: avatar,
                });
                if (out.length >= maxCount) break;
            }
            return out;
        }""",
        [testid_prefix, max_count],
    )

    for it in raw:
        cid = it.get("conversation_id") or ""
        parts = cid.split(":")
        other = None
        if len(parts) == 2 and self_id:
            other = next((p for p in parts if p != self_id), None)
        it["other_user_id"] = other
    return raw


async def _extract_inbox_dom(page: Page, max_count: int) -> list[dict[str, Any]]:
    """Scrape the main inbox conversation list."""
    return await _extract_dm_items(
        page, testid_prefix="dm-conversation-item-", max_count=max_count
    )


def _build_conv_id(user_a: str, user_b: str) -> str:
    """XChat conversation_id format: numerically smaller user_id first."""
    a, b = int(user_a), int(user_b)
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{lo}:{hi}"


def _conv_id_to_url_segment(conv_id: str) -> str:
    """The /i/chat/{...} path uses `-` instead of `:`."""
    return conv_id.replace(":", "-")


async def _open_conversation(page: Page, conv_id: str) -> None:
    """Navigate the SPA to a specific conversation page. Idempotent."""
    target_path = f"/i/chat/{_conv_id_to_url_segment(conv_id)}"
    if target_path in page.url:
        return
    # If we're already in /i/chat, prefer in-app navigation via inbox item click
    # (faster + matches the way the SPA hydrates). Fall back to a hard goto.
    if X_INBOX_PATH in page.url:
        try:
            await page.click(
                f'[data-testid="dm-conversation-item-{conv_id}"]',
                timeout=5000,
            )
            return
        except PWTimeout:
            pass
    try:
        await page.goto(
            f"{X_BASE}{target_path}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
    except PWTimeout:
        pass


async def _extract_conversation_dom(page: Page, count: int) -> list[dict[str, Any]]:
    """Scrape rendered messages from the active conversation panel.

    XChat encodes message events as base64+Thrift in GraphQL responses,
    which we cannot decode without the schema; the rendered DOM is the
    only practical source. Each message exposes:
        [data-testid="message-{uuid}"]          // outer container (full-width)
            [data-testid="message-text-{uuid}"] // text node (aligned bubble)

    `mine` is determined by horizontal alignment: own bubbles render
    right-aligned (with bg-primary), theirs left-aligned (bg-gray-50).
    Messages without a text element (media/attachment) get mine=null
    and text="" with kind="media".
    """
    raw = await page.evaluate(
        """(maxCount) => {
            const list = document.querySelector('[data-testid="dm-message-list"]');
            if (!list) return [];
            const lr = list.getBoundingClientRect();
            const out = [];
            const nodes = list.querySelectorAll('[data-testid^="message-"]:not([data-testid^="message-text-"])');
            nodes.forEach(el => {
                const testid = el.getAttribute('data-testid') || '';
                const messageId = testid.slice('message-'.length);
                const textEl = el.querySelector('[data-testid^="message-text-"]');

                let text = '';
                let mine = null;
                let kind = 'text';

                if (textEl) {
                    const raw = textEl.innerText || '';
                    const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
                    // Strip trailing duplicated time stamps that XChat appends.
                    while (lines.length > 1) {
                        const last = lines[lines.length - 1];
                        if (/^\\d{1,2}:\\d{2}(\\s*[AP]M)?$/i.test(last)) {
                            lines.pop();
                        } else {
                            break;
                        }
                    }
                    text = lines.join('\\n');

                    const tr = textEl.getBoundingClientRect();
                    const leftDist = tr.left - lr.left;
                    const rightDist = lr.right - tr.right;
                    if (Math.abs(leftDist - rightDist) > 30) {
                        mine = rightDist < leftDist;
                    }
                } else {
                    kind = 'media';
                    // Best-effort mine detection on the outer message div.
                    // The inner bubble (1-2 levels down) carries the alignment;
                    // walk down to the first child narrower than the list.
                    let bubble = el.firstElementChild;
                    while (bubble) {
                        const br = bubble.getBoundingClientRect();
                        if (br.width < lr.width * 0.95) {
                            const leftDist = br.left - lr.left;
                            const rightDist = lr.right - br.right;
                            if (Math.abs(leftDist - rightDist) > 30) {
                                mine = rightDist < leftDist;
                            }
                            break;
                        }
                        bubble = bubble.firstElementChild;
                    }
                }

                out.push({id: messageId, text: text, mine: mine, kind: kind});
            });
            // DOM order is chronological (oldest → newest). Return the most recent N.
            return out.slice(-maxCount);
        }""",
        count,
    )
    return raw


def _extract_user_id_from_user_by_screen_name(payload: dict[str, Any]) -> str | None:
    try:
        u = payload["data"]["user"]["result"]
        return u.get("rest_id") or (u.get("legacy") or {}).get("id_str")
    except Exception:
        return None


async def _resolve_user_id(page: Page, username: str) -> str | None:
    """Navigate to /{username} and capture the UserByScreenName GraphQL response."""
    capture = asyncio.create_task(
        _capture_first(page, _is_user_by_screen_name, timeout=20.0)
    )
    try:
        await page.goto(f"{X_BASE}/{username}", wait_until="domcontentloaded")
    except PWTimeout:
        pass
    try:
        payload = await capture
    except (asyncio.TimeoutError, PWTimeout):
        return None
    return _extract_user_id_from_user_by_screen_name(payload)


async def _ensure_logged_in(page: Page) -> None:
    if not await _is_logged_in(page):
        # Make sure we're on a page where login UI is visible
        try:
            await page.goto(f"{X_BASE}/home", wait_until="domcontentloaded")
        except Exception:
            pass
        if not await _is_logged_in(page):
            raise RuntimeError(
                "Not logged in. Run once with HEADLESS=0, log in via the browser "
                "window, then close it. The persistent profile keeps your session."
            )


@mcp.tool()
async def list_dm_conversations(max_count: int = 20) -> str:
    """List recent DM conversations from the inbox.

    Args:
        max_count: Max conversations to return (default 20).
    """
    try:
        page = await _ensure_browser()
        await _ensure_logged_in(page)

        if X_INBOX_PATH not in page.url:
            try:
                await page.goto(
                    f"{X_BASE}{X_INBOX_PATH}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except PWTimeout:
                pass

        try:
            await page.wait_for_selector(_INBOX_ITEM_SELECTOR, timeout=20000)
        except PWTimeout:
            return _err("Inbox didn't render any conversation items within 20s")

        items = await _extract_inbox_dom(page, max_count)
        return _ok(items)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


async def _scroll_request_list_until_stable(
    page: Page, target_count: int, settle_rounds: int = 3, max_wait_s: float = 60.0
) -> int:
    """[Compat] Scroll the requests list and return how many unique items
    we accumulated. Prefer `_collect_request_items` for the actual data.
    """
    items = await _collect_request_items(page, target_count, max_wait_s=max_wait_s)
    return len(items)


async def _collect_request_items(
    page: Page,
    target_count: int,
    *,
    max_wait_s: float = 60.0,
    settle_rounds: int = 3,
) -> list[dict[str, Any]]:
    """Walk the virtualized message-requests list, accumulating items as
    they scroll into view. XChat's virtualizer recycles offscreen DOM
    nodes, so we have to harvest each batch before scrolling further.
    """
    self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))
    seen: dict[str, dict[str, Any]] = {}
    deadline = asyncio.get_event_loop().time() + max_wait_s
    at_bottom_streak = 0
    last_seen_count = -1

    while True:
        batch = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('[data-testid^="dm-message-request-item-"]').forEach(el => {
                    const testid = el.getAttribute('data-testid') || '';
                    const convId = testid.slice('dm-message-request-item-'.length);
                    const raw = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    out.push({
                        conversation_id: convId,
                        name: raw[0] || null,
                        time_relative: raw[1] || null,
                        last_preview: raw.length > 2 ? raw.slice(2).join(' ') : null,
                        avatar: el.querySelector('img') ? el.querySelector('img').src : null,
                    });
                });
                const items = document.querySelectorAll('[data-testid^="dm-message-request-item-"]');
                let atBottom = false;
                let scroller = null;
                if (items.length) {
                    const last = items[items.length - 1];
                    scroller = last.parentElement;
                    while (scroller && scroller !== document.body) {
                        const cs = getComputedStyle(scroller);
                        if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                            && scroller.scrollHeight > scroller.clientHeight) {
                            break;
                        }
                        scroller = scroller.parentElement;
                    }
                    if (scroller && scroller !== document.body) {
                        // Trigger next batch by anchoring the last visible item to bottom.
                        last.scrollIntoView({block: 'end', behavior: 'auto'});
                        atBottom =
                            scroller.scrollTop + scroller.clientHeight + 4 >= scroller.scrollHeight;
                    }
                }
                return {items: out, atBottom};
            }"""
        )

        for it in batch.get("items") or []:
            cid = it.get("conversation_id")
            if not cid or cid in seen:
                continue
            parts = cid.split(":")
            other = None
            if len(parts) == 2 and self_id:
                other = next((p for p in parts if p != self_id), None)
            it["other_user_id"] = other
            seen[cid] = it

        count = len(seen)
        if count >= target_count:
            break

        if batch.get("atBottom") and count == last_seen_count:
            at_bottom_streak += 1
            if at_bottom_streak >= settle_rounds:
                break
        else:
            at_bottom_streak = 0
        last_seen_count = count

        if asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(0.6)

    out = list(seen.values())
    return out[:target_count]


@mcp.tool()
async def list_message_requests(
    tab: str = "priority", max_count: int = 50
) -> str:
    """List pending message requests (people you haven't accepted DMs from).

    These are the conversations under /i/chat/requests, sorted into two
    tabs by X's spam classifier:
      - "priority": likely-real requests X surfaces by default
      - "hidden":   the rest (often spam / cold outreach / fan messages)

    Args:
        tab: "priority" or "hidden" (default "priority").
        max_count: max items to return. XChat virtualizes the list, so
            asking for more triggers scroll-and-wait.
    """
    try:
        if tab not in ("priority", "hidden"):
            return _err("tab must be 'priority' or 'hidden'")

        page = await _ensure_browser()
        await _ensure_logged_in(page)

        # Boot the SPA via /i/chat first; opening /i/chat/requests cold
        # sometimes lands on a blank shell because the XChat bundle isn't
        # warm yet.
        if "/i/chat" not in page.url:
            try:
                await page.goto(
                    f"{X_BASE}{X_INBOX_PATH}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except PWTimeout:
                pass
            try:
                await page.wait_for_selector(_INBOX_ITEM_SELECTOR, timeout=15000)
            except PWTimeout:
                pass

        if X_REQUESTS_PATH not in page.url:
            try:
                await page.goto(
                    f"{X_BASE}{X_REQUESTS_PATH}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except PWTimeout:
                pass

        # Pick tab. Priority is the default landing tab; Hidden requires a click.
        if tab == "hidden":
            try:
                await page.click(_REQUEST_TAB_HIDDEN, timeout=8000)
            except PWTimeout:
                return _err("Couldn't find/click the Hidden tab")
            await asyncio.sleep(1)

        try:
            await page.wait_for_selector(_REQUEST_ITEM_SELECTOR, timeout=15000)
        except PWTimeout:
            # Empty tab is legitimate
            return _ok({"tab": tab, "count": 0, "items": []})

        items = await _collect_request_items(page, max_count, max_wait_s=60.0)
        return _ok({"tab": tab, "count": len(items), "items": items})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
async def read_dm_history(username: str = "", count: int = 50, conversation_id: str = "") -> str:
    """Read DM history with a given user.

    Args:
        username: Target user's screen name (no @). Either this or
            conversation_id must be provided.
        count: Max messages to return (most recent first in chronological
            order is preserved as DOM order).
        conversation_id: Optional. If known (e.g. from list_dm_conversations),
            pass it directly to skip user resolution — much faster.
    """
    try:
        page = await _ensure_browser()
        await _ensure_logged_in(page)

        conv_id = conversation_id
        other_user_id = None
        if not conv_id:
            if not username:
                return _err("Either username or conversation_id must be provided")
            user_id = await _resolve_user_id(page, username)
            if not user_id:
                return _err(f"Could not resolve user_id for @{username}")
            self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))
            if not self_id:
                return _err("Could not determine own user_id from cookies")
            conv_id = _build_conv_id(self_id, user_id)
            other_user_id = user_id

        await _open_conversation(page, conv_id)

        try:
            await page.wait_for_selector(_MESSAGE_LIST_SELECTOR, timeout=20000)
        except PWTimeout:
            return _err(
                "Message list didn't render within 20s. The conversation "
                "may not exist yet, or XChat selectors may have changed.",
                conversation_id=conv_id,
            )
        # XChat virtualizes the list; wait for renders to settle before scraping.
        await _wait_for_messages_stable(page, min_messages=1, settle_ms=1500, timeout_s=15.0)

        msgs = await _extract_conversation_dom(page, count)

        if other_user_id is None:
            self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))
            parts = conv_id.split(":")
            if len(parts) == 2 and self_id:
                other_user_id = next((p for p in parts if p != self_id), None)

        return _ok({
            "conversation_id": conv_id,
            "other_user_id": other_user_id,
            "messages": msgs,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


async def _send_in_active_conversation(
    page: Page, text: str, dry_run: bool = False
) -> dict[str, Any]:
    """Type text into the XChat composer of the currently-open conversation
    and submit. Returns {sent, cleared, dry_run} status info.

    Caller is responsible for ensuring `page` is on the right conversation
    and that the message list/composer have rendered.
    """
    try:
        composer = await page.wait_for_selector(
            _COMPOSER_TEXTAREA_SELECTOR, timeout=15000, state="visible"
        )
    except PWTimeout:
        raise RuntimeError(
            f"Composer textarea not found ({_COMPOSER_TEXTAREA_SELECTOR}). "
            "The page may not have loaded, or XChat selectors changed."
        )
    if composer is None:
        raise RuntimeError("Composer textarea element missing.")

    await composer.click()
    # Use `insert_text` to preserve newlines/emoji rather than typing key by key
    # (typing simulates each key event; multi-codepoint emoji may misbehave).
    await page.keyboard.insert_text(text)

    if dry_run:
        return {"sent": False, "cleared": False, "dry_run": True}

    # XChat: Enter submits, Shift+Enter inserts newline. (Matches the
    # default behavior we observed in the rendered composer.)
    await page.keyboard.press("Enter")

    # Confirm send by watching the composer drain.
    cleared = False
    try:
        await page.wait_for_function(
            """sel => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const v = (el.value !== undefined) ? el.value : (el.innerText || '');
                return v.trim() === '';
            }""",
            arg=_COMPOSER_TEXTAREA_SELECTOR,
            timeout=8000,
        )
        cleared = True
    except PWTimeout:
        cleared = False
    return {"sent": True, "cleared": cleared, "dry_run": False}


@mcp.tool()
async def send_dm(username: str = "", text: str = "", conversation_id: str = "") -> str:
    """Send a DM to a user via the on-page XChat composer.

    Args:
        username: Target user's screen name (no @). Either this or
            conversation_id must be provided.
        text: Message body.
        conversation_id: Optional. If known (e.g. from list_dm_conversations),
            pass it directly to skip user resolution — much faster.
    """
    try:
        if not text:
            return _err("text is required")

        page = await _ensure_browser()
        await _ensure_logged_in(page)

        conv_id = conversation_id
        user_id = None
        if not conv_id:
            if not username:
                return _err("Either username or conversation_id must be provided")
            user_id = await _resolve_user_id(page, username)
            if not user_id:
                return _err(f"Could not resolve user_id for @{username}")
            self_id = _self_user_id_from_cookies(await page.context.cookies(X_BASE))
            if not self_id:
                return _err("Could not determine own user_id from cookies")
            conv_id = _build_conv_id(self_id, user_id)

        # Make sure the SPA is in /i/chat so in-app navigation works.
        if X_INBOX_PATH not in page.url and "/i/chat" not in page.url:
            try:
                await page.goto(
                    f"{X_BASE}{X_INBOX_PATH}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except PWTimeout:
                pass

        await _open_conversation(page, conv_id)

        try:
            await page.wait_for_selector(_MESSAGE_LIST_SELECTOR, timeout=15000)
        except PWTimeout:
            return _err("Conversation panel didn't render.", conversation_id=conv_id)

        status = await _send_in_active_conversation(page, text, dry_run=False)
        return _ok({
            "conversation_id": conv_id,
            "user_id": user_id,
            **status,
            "note": (
                "composer cleared, message likely sent"
                if status.get("cleared")
                else "send issued but couldn't confirm via UI; verify with read_dm_history"
            ),
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
async def get_my_info() -> str:
    """Return basic info about the currently logged-in account."""
    try:
        page = await _ensure_browser()
        await _ensure_logged_in(page)

        capture = asyncio.create_task(
            _capture_first(page, _is_viewer_response, timeout=15.0)
        )
        try:
            await page.goto(f"{X_BASE}/home", wait_until="domcontentloaded")
        except PWTimeout:
            pass

        try:
            payload = await capture
        except (asyncio.TimeoutError, PWTimeout):
            payload = None

        # Cookies always tell us we're logged in; pull screen_name from twid cookie.
        cookies = await page.context.cookies(X_BASE)
        screen_name = None
        user_id = None
        for c in cookies:
            if c.get("name") == "twid":
                # twid is like "u%3D123456789"
                v = c.get("value", "")
                if "u%3D" in v:
                    user_id = v.split("u%3D", 1)[1]
                elif "u=" in v:
                    user_id = v.split("u=", 1)[1]

        info: dict[str, Any] = {"user_id": user_id, "screen_name": screen_name}
        if isinstance(payload, dict):
            # /account/settings.json case
            if "screen_name" in payload:
                info["screen_name"] = payload.get("screen_name")
                info["language"] = payload.get("language")
            # Viewer GraphQL case
            try:
                v = payload["data"]["viewer"]["user_results"]["result"]
                legacy = v.get("legacy") or {}
                info["user_id"] = info["user_id"] or v.get("rest_id")
                info["screen_name"] = info["screen_name"] or legacy.get("screen_name")
                info["name"] = legacy.get("name")
            except Exception:
                pass
            info["raw_keys"] = list(payload.keys())[:8]
        return _ok(info)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        mcp.run(transport="stdio")
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(_shutdown())
        except Exception:
            pass
