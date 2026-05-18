# Twitter DM MCP (Playwright + XChat DOM)

> [中文版](README.md)

Read / send Twitter/X direct messages from Claude Code (or any MCP host), backed by a real Chromium driven via Playwright with a persistent profile. No official API, no `twikit`.

Includes a Chinese-localized terminal UI (`dm_tui.py`) for incremental review of new and updated DMs / message requests, plus standalone scripts for one-off scrapes and AI-summarization exports.

## Why a real browser

X rewrote DMs as **XChat**, a Kotlin Multiplatform app whose conversation state lives in IndexedDB and syncs via Thrift-encoded events over GraphQL. There is no longer a single `inbox_initial_state.json` to intercept — earlier scraping libraries broke and reverse-engineering the Thrift schema isn't practical. Driving the rendered UI is the only reliable way:

- **Inbox + message-requests:** scraped from `[data-testid^="dm-conversation-item-"]` / `dm-message-request-item-`
- **Conversation history:** scraped from `[data-testid^="message-text-"]` (each bubble's alignment infers `mine` vs `theirs`)
- **Sending:** typed into `[data-testid="dm-composer-textarea"]` + Enter to submit

The cost is a Chromium process running headed during scrapes (≈ 300 MB RSS). X frequently blocks headless mode, so `--headed` is the default in every script.

## Install

```powershell
pip install -r requirements.txt
playwright install chromium
```

Python 3.11+.

## First-time login

The persistent profile lives at `~/.twitter-dm-mcp/browser-data` (override with `TWITTER_PROFILE`). One-time setup:

```powershell
$env:TWITTER_PROXY = "http://127.0.0.1:7897"   # only if x.com is blocked
python login.py
```

A Chromium window opens at `https://x.com/home`. Complete the login flow once; the script auto-detects when the `auth_token` cookie is set and exits. From then on the profile is reused.

If the script reports `Found a stale auth_token cookie - x.com bounced us to login`, log in again — your old cookie expired.

## Layout

| File | Role |
|---|---|
| `server.py` | FastMCP server (stdio) exposing 4 tools |
| `login.py` | Interactive first-time login |
| `dm_check.py` | Incremental scraper: latest 50 per source (chat / priority / hidden), diffed against `~/.twitter-dm-mcp/dm_state.json`, emits `dm_check.json` |
| `dm_tui.py` | Textual TUI: tabbed viewer with settings page + live progress log |
| `read_inbox.py` | Standalone: list main inbox conversations |
| `read_history.py` | Standalone: print one conversation's message history |
| `read_requests.py` | Standalone: scrape Priority / Hidden requests |
| `send_test.py` | Standalone: dry-run or send a DM |
| `export_requests.py` | One-shot full export of both Requests tabs |

## MCP tools

Mount the server in Claude Code:

```powershell
claude mcp add twitter-dm `
  --env HEADLESS=1 `
  --env TWITTER_PROXY=http://127.0.0.1:7897 `
  -- python "C:\path\to\twitter-dm-mcp\server.py"
```

The TUI's **Settings → MCP integration** section can build and copy this command for you.

| Tool | Description |
|---|---|
| `list_dm_conversations(max_count=20)` | Scrape recent inbox conversations from `/i/chat` (name, time, last preview, conversation_id, other_user_id) |
| `read_dm_history(username="", count=50, conversation_id="")` | Open one conversation and return its rendered messages with `mine`/`them` direction inferred from bubble alignment |
| `send_dm(username="", text="", conversation_id="")` | Type into the XChat composer and submit with Enter |
| `list_message_requests(tab="priority", max_count=50)` | Pending DM requests from `/i/chat/requests`. `tab` ∈ {`priority`, `hidden`} |
| `get_my_info()` | Logged-in account id + screen name (from cookies + Viewer API) |

All tools return JSON strings: `{"ok": true, "data": ...}` or `{"ok": false, "error": "..."}`. Tools never raise out.

## TUI workflow

```powershell
python dm_tui.py
```

Four tabs — **聊天 / 优先 / 隐藏 / 设置** — plus a live log panel at the bottom.

Keys:

| Key | Action |
|---|---|
| `r` | Refresh (runs `dm_check.py` as subprocess; stderr streams to the log) |
| `e` | Export current state as timestamped JSON |
| `f` | Toggle: show changes only vs. all 50 scanned per source |
| `h` | Toggle headed/headless scrape mode (default headed; X blocks headless) |
| `1` / `2` / `3` / `4` | Jump to chat / priority / hidden / settings |
| `↑` / `↓` | Move selection |
| `Enter` | Open selected conversation in default browser |
| `Ctrl+L` | Clear log |
| `q` | Quit |

**Settings page** persists to `~/.twitter-dm-mcp/tui_config.json`:

- **Proxy** — auto-applied as `TWITTER_PROXY` env to every subprocess
- **Login** — launches `login.py` with streaming output
- **Scheduled refresh** — every N minutes; uses Textual's `set_interval`
- **MCP integration** — generates and copies the `claude mcp add` command (or runs it directly)
- **Export** — directory + three modes: all sources / current tab / changes only

## Standalone scripts

Each runs independently with the same env vars.

```powershell
# Inbox
python read_inbox.py 20 --headed

# A specific conversation
python read_history.py "1441009715782115342:1552213521253150720" 30 --headed

# Message requests
python read_requests.py priority --headed
python read_requests.py hidden 500 --raw --headed > hidden.json

# Send (dry-run first)
python send_test.py "<conv_id>" "测试" --headed                # dry-run, no Enter
python send_test.py "<conv_id>" "测试" --send --headed         # actually sends

# Full export of both Requests tabs in one file
python export_requests.py --hidden-max 800 --headed
```

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `HEADLESS` | `1` | `0` shows the browser. Required for first-time login. Scripts that ship `--headed` flags override this. |
| `TWITTER_PROXY` | (none) | e.g. `http://127.0.0.1:7897`. Forwarded to Playwright's `proxy.server`. The TUI settings page also writes this. |
| `TWITTER_PROFILE` | `~/.twitter-dm-mcp/browser-data` | Override the persistent user-data-dir. |

## Files written at runtime (gitignored)

| Path | What |
|---|---|
| `~/.twitter-dm-mcp/browser-data/` | Persistent Chromium profile (cookies, IndexedDB, ...) |
| `~/.twitter-dm-mcp/dm_state.json` | Last-seen `{conv_id: preview}` per source, for incremental diff |
| `~/.twitter-dm-mcp/tui_config.json` | TUI settings (proxy, schedule, MCP service name, export dir) |
| `./dm_check.json` | Output of the latest `dm_check.py` run |
| `./dm_export_*_<timestamp>.json` | Snapshots saved by `e` in TUI or the export buttons |

## Known limitations

- **Headed required.** X consistently blocks headless mode for the XChat pages — `goto /i/chat/requests` times out or returns a blank shell. Every script defaults to `--headed`.
- **No precise timestamps from DM messages.** XChat only renders relative time (`13:35`, `4月23日周四`, `1w`). Unix-epoch times live in Thrift-encoded GraphQL responses we can't decode without the schema.
- **No sender_id on individual messages.** Direction is inferred from bubble horizontal alignment (own bubble bg-primary right-aligned vs theirs bg-gray-50 left-aligned). Media-only messages get `mine=null`, `kind="media"`.
- **Hidden Requests reach ~440-510 even when X reports 548.** Some entries are stale / hidden behind further scrolling. Adjust `--hidden-max-wait`.
- **High-frequency scrapes risk rate-limiting your account.** A scheduled refresh of 5 minutes is usually fine; sub-minute is asking for trouble.
- **UI test-ids can change.** XChat is actively developed; if a selector breaks (most likely the composer or message bubble structure), update the constants near the top of `server.py`.

## License

MIT. See `LICENSE` if present, or treat this as MIT.
