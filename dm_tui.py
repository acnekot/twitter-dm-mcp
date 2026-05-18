"""Textual TUI 中文界面 + 实时日志面板 + 设置页。

读取 `dm_check.json`,分四个标签页(聊天 / 优先 / 隐藏 / 设置),
左侧列表+右侧详情,底部显示运行日志。

快捷键:
  q       退出
  r       刷新
  e       导出全部到带时间戳的 JSON
  h       切换浏览器可见(默认可见,X 在 headless 下会拦截)
  f       切换显示模式:只看变化 / 全部展示(含未变项)
  1/2/3/4 跳转 聊天 / 优先 / 隐藏 / 设置
  ↑/↓     移动选中
  Enter   在浏览器中打开当前会话
  Ctrl+L  清空日志

用法:
    python dm_tui.py
    python dm_tui.py --json other.json
    python dm_tui.py --refresh-on-start
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

SOURCES = ("chat", "priority", "hidden")
SOURCE_LABELS = {"chat": "聊天", "priority": "优先", "hidden": "隐藏"}

PROFILE_ROOT = Path.home() / ".twitter-dm-mcp"
CONFIG_PATH = PROFILE_ROOT / "tui_config.json"


# ---------- 配置 ---------------------------------------------------------


@dataclass
class TuiConfig:
    proxy: str = ""
    auto_refresh_minutes: int = 0  # 0 = 关闭
    auto_refresh_enabled: bool = False
    mcp_server_name: str = "twitter-dm"
    export_dir: str = ""  # 空 = 工作目录

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "TuiConfig":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**valid)
        except Exception:
            return cls()

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_check_json(path: Path) -> dict:
    if not path.exists():
        return {"fetched_at": None, "sources": {s: {} for s in SOURCES}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e), "sources": {s: {} for s in SOURCES}}


def flatten_source(src_data: dict, include_unchanged: bool) -> list[dict]:
    out: list[dict] = []
    for it in src_data.get("new") or []:
        out.append({**it, "change": it.get("change") or "new"})
    for it in src_data.get("updated") or []:
        out.append({**it, "change": it.get("change") or "updated"})
    if include_unchanged:
        for it in src_data.get("unchanged") or []:
            out.append({**it, "change": it.get("change") or "unchanged"})
    return out


def render_item_label(item: dict) -> Text:
    change = item.get("change") or ""
    badge_map = {
        "new": ("● 新", "bold green"),
        "updated": ("◆ 改", "bold yellow"),
        "unchanged": ("· 旧", "dim"),
    }
    badge_text, badge_style = badge_map.get(change, ("·", "dim"))

    name = item.get("name") or "(无名)"
    t = item.get("time_relative") or ""
    preview = (item.get("last_preview") or "").replace("\n", " ")
    if len(preview) > 90:
        preview = preview[:87] + "..."

    line = Text()
    line.append(f"{badge_text:<5}", style=badge_style)
    line.append(f"{name}  ", style="bold" if change != "unchanged" else "dim")
    if t:
        line.append(f"[{t}]  ", style="dim cyan")
    line.append(preview, style="dim" if change == "unchanged" else "")
    return line


def render_detail(item: dict | None) -> Text:
    if not item:
        return Text("(请选择一条会话)", style="dim italic")
    out = Text()
    change = item.get("change") or "unchanged"
    label_map = {"new": "新增", "updated": "更新", "unchanged": "未变"}
    style = {"new": "bold green", "updated": "bold yellow"}.get(change, "dim")
    out.append(f"{label_map.get(change, change)}\n\n", style=style)

    out.append(f"{item.get('name') or '(无名)'}\n", style="bold")

    out.append("时间:    ", style="dim")
    out.append(f"{item.get('time_relative') or '?'}\n")

    out.append("会话 ID: ", style="dim")
    out.append(f"{item.get('conversation_id') or '?'}\n")

    out.append("对方 ID: ", style="dim")
    out.append(f"{item.get('other_user_id') or '?'}\n")

    out.append("\n最新预览:\n", style="dim")
    out.append((item.get("last_preview") or "").strip() + "\n")

    if change == "updated" and item.get("previous_preview"):
        out.append("\n上次预览:\n", style="dim yellow")
        out.append(item["previous_preview"].strip() + "\n", style="yellow")

    avatar = item.get("avatar")
    if avatar:
        out.append(f"\n头像: {avatar}\n", style="dim")
    return out


# ---------- 标签页:数据源 ------------------------------------------------


class SourcePane(Vertical):
    DEFAULT_CSS = """
    SourcePane { layout: vertical; }
    SourcePane > #status { height: 1; color: $text-muted; padding: 0 1; }
    SourcePane > Horizontal { height: 1fr; }
    SourcePane ListView { width: 60%; border: solid $primary; }
    SourcePane #detail { width: 40%; border: solid $accent; padding: 1; overflow-y: auto; }
    """

    def __init__(self, source: str, **kw) -> None:
        super().__init__(**kw)
        self.source = source
        self.items: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal():
            yield ListView(id="list")
            yield Static("", id="detail", expand=True)

    def update_data(self, src_data: dict, show_only_changes: bool) -> None:
        self.items = flatten_source(src_data, include_unchanged=not show_only_changes)
        unchanged = src_data.get("unchanged_count", 0)
        total = src_data.get("total_scanned", 0)
        new_n = len(src_data.get("new") or [])
        upd_n = len(src_data.get("updated") or [])

        mode = "只看变化" if show_only_changes else "全部展示"
        status = self.query_one("#status", Static)
        status.update(
            f"[{mode}]  新增 +{new_n}   更新 ~{upd_n}   未变 ={unchanged}   "
            f"(本次扫描 {total},当前列出 {len(self.items)})"
        )

        list_view = self.query_one("#list", ListView)
        list_view.clear()
        for it in self.items:
            list_view.append(ListItem(Static(render_item_label(it))))

        self.query_one("#detail", Static).update(render_detail(None))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None or idx < 0 or idx >= len(self.items):
            return
        self.query_one("#detail", Static).update(render_detail(self.items[idx]))

    def open_selected(self) -> bool:
        idx = self.query_one("#list", ListView).index
        if idx is None or idx < 0 or idx >= len(self.items):
            return False
        conv = self.items[idx].get("conversation_id") or ""
        if not conv:
            return False
        url = f"https://x.com/i/chat/{conv.replace(':', '-')}"
        webbrowser.open(url)
        return True


# ---------- 标签页:设置 -------------------------------------------------


class SettingsPane(VerticalScroll):
    """设置页 — 代理 / 登录 / 定时 / MCP 集成。"""

    DEFAULT_CSS = """
    SettingsPane { padding: 1 2; }

    SettingsPane > Vertical {
        height: auto;
        margin-bottom: 1;
        padding: 1 2;
        border: round $primary;
    }

    SettingsPane .title {
        color: $accent;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    SettingsPane .hint {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    SettingsPane .status {
        color: $text-muted;
        height: auto;
        margin-top: 1;
    }

    SettingsPane Horizontal {
        height: auto;
        margin-bottom: 0;
    }
    SettingsPane .lbl {
        width: 16;
        height: 3;
        content-align: left middle;
        padding: 0 1 0 0;
    }
    SettingsPane Input { width: 1fr; }
    SettingsPane Button { margin-left: 1; min-width: 12; }

    SettingsPane #mcp-cmd {
        border: solid $accent;
        padding: 1;
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
    }
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)

    def compose(self) -> ComposeResult:
        # 代理
        with Vertical():
            yield Static("代理设置", classes="title")
            yield Static(
                "用于 Playwright 抓取 X 时走代理。子进程会自动用这个值覆盖 TWITTER_PROXY。",
                classes="hint",
            )
            with Horizontal():
                yield Static("代理 URL:", classes="lbl")
                yield Input(placeholder="http://127.0.0.1:7897", id="proxy-input")
                yield Button("保存", id="proxy-save", variant="primary")
            yield Static("", id="proxy-status", classes="status")

        # 登录
        with Vertical():
            yield Static("登录", classes="title")
            yield Static(
                "首次使用或 cookie 失效时,启动 login.py 在弹出的 chromium 里手动登录。",
                classes="hint",
            )
            with Horizontal():
                yield Button("启动登录", id="login-start", variant="warning")
                yield Button("检查登录状态", id="login-check")
            yield Static("", id="login-status", classes="status")

        # 定时刷新
        with Vertical():
            yield Static("定时刷新", classes="title")
            yield Static(
                "每隔 N 分钟自动按一次刷新。设为 0 或关闭都会停止定时器。",
                classes="hint",
            )
            with Horizontal():
                yield Static("间隔(分钟):", classes="lbl")
                yield Input(placeholder="0", id="interval-input")
                yield Button("启用", id="interval-toggle", variant="success")
            yield Static("", id="interval-status", classes="status")

        # MCP 集成
        with Vertical():
            yield Static("MCP 集成", classes="title")
            yield Static(
                "把 server.py 注册为 Claude Code 的 MCP 服务器。点击复制后粘贴到 PowerShell 即可。",
                classes="hint",
            )
            with Horizontal():
                yield Static("服务名:", classes="lbl")
                yield Input(placeholder="twitter-dm", id="mcp-name-input")
            yield Static("(注册命令)", id="mcp-cmd")
            with Horizontal():
                yield Button("复制命令到剪贴板", id="mcp-copy", variant="primary")
                yield Button("尝试直接注册", id="mcp-register")
            yield Static("", id="mcp-status", classes="status")

        # 导出
        with Vertical():
            yield Static("导出 JSON", classes="title")
            yield Static(
                "把当前数据保存为带时间戳的 JSON 文件,方便丢给 AI 总结或归档。",
                classes="hint",
            )
            with Horizontal():
                yield Static("导出目录:", classes="lbl")
                yield Input(placeholder="留空 = 工作目录", id="export-dir-input")
                yield Button("保存", id="export-dir-save")
            with Horizontal():
                yield Button("导出全部", id="export-all", variant="primary")
                yield Button("仅当前 tab", id="export-current")
                yield Button("仅变化", id="export-changes")
            yield Static("提示:主界面按 e 快速导出全部到上面目录。", classes="hint")
            yield Static("", id="export-status", classes="status")

    def populate(self, config: TuiConfig, login_state: str, mcp_cmd: str) -> None:
        self.query_one("#proxy-input", Input).value = config.proxy
        self.query_one("#proxy-status", Static).update(
            f"当前生效: {config.proxy or '(未设置)'}"
        )

        self.query_one("#interval-input", Input).value = str(config.auto_refresh_minutes)
        btn = self.query_one("#interval-toggle", Button)
        btn.label = "停用" if config.auto_refresh_enabled else "启用"
        btn.variant = "error" if config.auto_refresh_enabled else "success"
        self.query_one("#interval-status", Static).update(
            f"状态: {'已启用,每 ' + str(config.auto_refresh_minutes) + ' 分钟刷新' if config.auto_refresh_enabled else '未启用'}"
        )

        self.query_one("#login-status", Static).update(login_state)

        self.query_one("#mcp-name-input", Input).value = config.mcp_server_name
        self.query_one("#mcp-cmd", Static).update(mcp_cmd)

        self.query_one("#export-dir-input", Input).value = config.export_dir


# ---------- 主应用 ------------------------------------------------------


class DMViewer(App):
    CSS = """
    Screen { background: $surface; }
    #main { height: 1fr; }
    TabbedContent { height: 1fr; }
    #log { height: 8; border-top: solid $accent; background: $boost; }
    #log.refreshing { border-top: solid $warning; }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("r", "refresh", "刷新"),
        Binding("e", "export('all')", "导出全部"),
        Binding("h", "toggle_headed", "切换浏览器可见"),
        Binding("f", "toggle_filter", "只变化/全部"),
        Binding("1", "tab('chat')", "聊天"),
        Binding("2", "tab('priority')", "优先"),
        Binding("3", "tab('hidden')", "隐藏"),
        Binding("4", "tab('settings')", "设置"),
        Binding("enter", "open_selected", "浏览器打开"),
        Binding("ctrl+l", "clear_log", "清空日志"),
    ]

    show_only_changes: reactive[bool] = reactive(False)
    is_refreshing: reactive[bool] = reactive(False)
    is_login_running: reactive[bool] = reactive(False)
    headed_scrape: reactive[bool] = reactive(True)

    def __init__(self, json_path: Path, refresh_on_start: bool, headed: bool = True) -> None:
        super().__init__()
        self.json_path = json_path
        self.refresh_on_start = refresh_on_start
        self.headed_scrape = headed
        self.config = TuiConfig.load()
        self.data: dict = {}
        self._interval_timer: Timer | None = None

    # ----- 子进程 env 构造:让 config.proxy 覆盖 -----

    def _subprocess_env(self) -> dict:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        if self.config.proxy:
            env["TWITTER_PROXY"] = self.config.proxy
        env["HEADLESS"] = "0" if self.headed_scrape else "1"
        return env

    # ----- compose / mount -----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            with TabbedContent(initial="tab-chat", id="tabs"):
                for src in SOURCES:
                    with TabPane(SOURCE_LABELS[src], id=f"tab-{src}"):
                        yield SourcePane(src, id=f"pane-{src}")
                with TabPane("设置", id="tab-settings"):
                    yield SettingsPane(id="pane-settings")
            yield RichLog(id="log", highlight=True, markup=False, wrap=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "Twitter DM 查看器"
        log = self.query_one("#log", RichLog)
        log.write(self._stamp(), shrink=False)
        self.log_line(f"加载 JSON: {self.json_path}")
        self.log_line(f"配置文件: {CONFIG_PATH}")
        self._reload_data()
        self._refresh_settings_pane()
        self._apply_interval_timer()
        if self.refresh_on_start:
            self.log_line("启动时自动刷新...")
            self.action_refresh()
        else:
            self.log_line("就绪。按 r 刷新,4 进入设置,q 退出。")

    @staticmethod
    def _stamp() -> str:
        return f"=== Twitter DM 查看器 启动于 {datetime.now().strftime('%H:%M:%S')} ==="

    def log_line(self, msg: str, *, style: str | None = None) -> None:
        log = self.query_one("#log", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        text = Text()
        text.append(f"[{ts}] ", style="dim")
        text.append(msg, style=style or "")
        log.write(text)

    # ----- 数据加载 -----

    def _reload_data(self) -> None:
        self.data = load_check_json(self.json_path)
        if "error" in self.data:
            self.log_line(f"读取 JSON 失败: {self.data['error']}", style="bold red")
        sources = self.data.get("sources") or {}
        for src in SOURCES:
            pane = self.query_one(f"#pane-{src}", SourcePane)
            pane.update_data(sources.get(src) or {}, self.show_only_changes)

        fetched = self.data.get("fetched_at") or "(未运行)"
        first = self.data.get("is_first_run")
        n = sum(len((sources.get(s) or {}).get("new") or []) for s in SOURCES)
        u = sum(len((sources.get(s) or {}).get("updated") or []) for s in SOURCES)
        suffix = "  [首次运行]" if first else ""
        self.sub_title = f"抓取于 {fetched}   合计 新增+{n} 更新~{u}{suffix}"

        tabs = self.query_one("#tabs", TabbedContent)
        for src in SOURCES:
            sd = sources.get(src) or {}
            n_new = len(sd.get("new") or [])
            n_upd = len(sd.get("updated") or [])
            label = SOURCE_LABELS[src]
            if n_new or n_upd:
                label = f"{label}  +{n_new}/~{n_upd}"
            try:
                tabs.get_tab(f"tab-{src}").label = label
            except Exception:
                pass

        self.log_line(f"已加载: 合计 新增 {n}, 更新 {u}, 抓取时间 {fetched}")

    # ----- 刷新流程 -----

    def action_refresh(self) -> None:
        if self.is_refreshing:
            self.notify("刷新正在进行中,请等待。", severity="warning")
            return
        self.is_refreshing = True
        log = self.query_one("#log", RichLog)
        log.add_class("refreshing")
        mode = "可见 chromium 窗口" if self.headed_scrape else "headless(X 经常拦截)"
        proxy = self.config.proxy or "(无代理)"
        self.log_line(
            f"开始刷新 → dm_check.py [{mode}] proxy={proxy}", style="bold cyan"
        )
        self._stream_refresh()

    @work(exclusive=True, thread=True, group="refresh")
    def _stream_refresh(self) -> None:
        cmd = [
            sys.executable,
            "-u",
            str(Path(__file__).parent / "dm_check.py"),
            "--out",
            str(self.json_path),
        ]
        if self.headed_scrape:
            cmd.append("--headed")
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                env=self._subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
        except Exception as e:
            self.call_from_thread(self._on_refresh_done, False, f"启动失败: {e}", 0.0)
            return

        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            style = self._classify_log_line(line)
            self.call_from_thread(self.log_line, line, style=style)

        rc = proc.wait()
        dt = time.monotonic() - t0
        self.call_from_thread(self._on_refresh_done, rc == 0, f"退出码 {rc}", dt)

    @staticmethod
    def _classify_log_line(line: str) -> str | None:
        low = line.lower()
        if "error" in low or "failed" in low or "traceback" in low:
            return "bold red"
        if "warn" in low:
            return "yellow"
        if line.startswith("  got ") or "collected " in low or "wrote " in low:
            return "green"
        if line.startswith("gather "):
            return "bold cyan"
        return None

    def _on_refresh_done(self, ok: bool, msg: str, elapsed: float) -> None:
        self.is_refreshing = False
        self.query_one("#log", RichLog).remove_class("refreshing")
        if ok:
            self.log_line(
                f"刷新完成 ({elapsed:.1f}s) — {msg}", style="bold green"
            )
            self._reload_data()
        else:
            self.log_line(
                f"刷新失败 ({elapsed:.1f}s) — {msg}", style="bold red"
            )

    # ----- 登录流程 -----

    @work(exclusive=True, thread=True, group="login")
    def _stream_login(self) -> None:
        cmd = [sys.executable, "-u", str(Path(__file__).parent / "login.py")]
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                env=self._subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
        except Exception as e:
            self.call_from_thread(self._on_login_done, False, f"启动失败: {e}", 0.0)
            return

        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            style = self._classify_log_line(line)
            self.call_from_thread(self.log_line, f"[login] {line}", style=style)

        rc = proc.wait()
        dt = time.monotonic() - t0
        self.call_from_thread(self._on_login_done, rc == 0, f"退出码 {rc}", dt)

    def _on_login_done(self, ok: bool, msg: str, elapsed: float) -> None:
        self.is_login_running = False
        status = self.query_one("#login-status", Static)
        if ok:
            self.log_line(f"登录脚本完成 ({elapsed:.1f}s) — {msg}", style="bold green")
            status.update(f"✓ 上次登录脚本成功完成 ({elapsed:.1f}s)")
        else:
            self.log_line(f"登录脚本失败 ({elapsed:.1f}s) — {msg}", style="bold red")
            status.update(f"✗ 上次失败: {msg}")

    # ----- 定时刷新 -----

    def _apply_interval_timer(self) -> None:
        if self._interval_timer is not None:
            try:
                self._interval_timer.stop()
            except Exception:
                pass
            self._interval_timer = None
        if (
            self.config.auto_refresh_enabled
            and self.config.auto_refresh_minutes > 0
        ):
            seconds = self.config.auto_refresh_minutes * 60
            self._interval_timer = self.set_interval(seconds, self._auto_refresh_tick)
            self.log_line(
                f"已启用定时刷新: 每 {self.config.auto_refresh_minutes} 分钟",
                style="bold magenta",
            )
        else:
            self.log_line("定时刷新已停用。", style="dim")

    def _auto_refresh_tick(self) -> None:
        if self.is_refreshing:
            self.log_line("定时刷新跳过:上次刷新还没结束。", style="yellow")
            return
        self.log_line("定时触发刷新。", style="bold magenta")
        self.action_refresh()

    # ----- MCP 命令 -----

    def _build_mcp_command(self) -> str:
        server_py = (Path(__file__).parent / "server.py").resolve()
        name = self.config.mcp_server_name or "twitter-dm"
        proxy = self.config.proxy
        # 在 PowerShell 里 backtick 续行;为简洁起见输出单行
        parts = [
            "claude mcp add",
            name,
            "--env HEADLESS=1",
        ]
        if proxy:
            parts.append(f'--env TWITTER_PROXY="{proxy}"')
        parts.append(f'-- python "{server_py}"')
        return " ".join(parts)

    def _copy_to_clipboard(self, text: str) -> bool:
        if sys.platform == "win32":
            try:
                p = subprocess.run(
                    ["clip"], input=text, text=True, encoding="utf-16-le", check=False
                )
                return p.returncode == 0
            except Exception:
                return False
        # macOS / Linux fallback
        for tool in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
            if shutil.which(tool[0]) is None:
                continue
            try:
                p = subprocess.run(tool, input=text, text=True, check=False)
                if p.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    @work(exclusive=True, thread=True, group="mcp")
    def _register_mcp(self, cmd: str) -> None:
        if shutil.which("claude") is None:
            self.call_from_thread(
                self.log_line,
                "找不到 `claude` 命令 — 请先安装 Claude Code,然后手动运行命令。",
                style="bold red",
            )
            return
        # 用 shell=True 在 Windows 上比较稳(claude 经常是 .cmd shim)
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as e:
            self.call_from_thread(
                self.log_line, f"注册失败: {e}", style="bold red"
            )
            return
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n").rstrip("\r")
            if line:
                self.call_from_thread(self.log_line, f"[mcp] {line}")
        rc = proc.wait()
        if rc == 0:
            self.call_from_thread(
                self.log_line, "MCP 注册成功!", style="bold green"
            )
        else:
            self.call_from_thread(
                self.log_line, f"MCP 注册失败 (退出码 {rc})", style="bold red"
            )

    # ----- 设置 pane 状态 -----

    def _refresh_settings_pane(self) -> None:
        try:
            pane = self.query_one("#pane-settings", SettingsPane)
        except Exception:
            return
        login_state = self._compute_login_state()
        pane.populate(self.config, login_state, self._build_mcp_command())

    def _compute_login_state(self) -> str:
        # 简易检测:profile 目录是否存在 + 是否有 cookies 文件
        prof = PROFILE_ROOT / "browser-data"
        if not prof.exists():
            return "未发现浏览器 profile (首次使用请点 '启动登录')"
        # 看 cookies 文件大小作为粗略指示
        cookies_path = prof / "Default" / "Network" / "Cookies"
        if cookies_path.exists():
            try:
                size = cookies_path.stat().st_size
                mtime = datetime.fromtimestamp(cookies_path.stat().st_mtime)
                return (
                    f"profile 存在,cookies 文件 {size:,} 字节,"
                    f"上次修改 {mtime.strftime('%Y-%m-%d %H:%M')}"
                )
            except Exception:
                pass
        return "profile 存在,但找不到 cookies 文件"

    # ----- 按钮 / 输入事件 -----

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "proxy-save":
            value = self.query_one("#proxy-input", Input).value.strip()
            self.config.proxy = value
            self.config.save()
            self.log_line(f"代理已保存: {value or '(空,不走代理)'}", style="bold green")
            self._refresh_settings_pane()

        elif bid == "login-start":
            if self.is_login_running:
                self.notify("登录脚本已经在跑了。", severity="warning")
                return
            self.is_login_running = True
            self.log_line("启动 login.py 子进程...", style="bold cyan")
            self.query_one("#login-status", Static).update("⏳ 登录脚本运行中,请在弹出的 chromium 中完成登录")
            self._stream_login()

        elif bid == "login-check":
            self.query_one("#login-status", Static).update(self._compute_login_state())
            self.log_line("已刷新登录状态。")

        elif bid == "interval-toggle":
            raw = self.query_one("#interval-input", Input).value.strip()
            try:
                minutes = int(raw or "0")
            except ValueError:
                self.notify("间隔必须是整数分钟。", severity="error")
                return
            if minutes < 0:
                minutes = 0
            self.config.auto_refresh_minutes = minutes
            if minutes == 0:
                self.config.auto_refresh_enabled = False
            else:
                self.config.auto_refresh_enabled = not self.config.auto_refresh_enabled
            self.config.save()
            self._apply_interval_timer()
            self._refresh_settings_pane()

        elif bid == "mcp-copy":
            name = self.query_one("#mcp-name-input", Input).value.strip() or "twitter-dm"
            self.config.mcp_server_name = name
            self.config.save()
            cmd = self._build_mcp_command()
            ok = self._copy_to_clipboard(cmd)
            self.log_line(
                f"{'已复制 MCP 命令到剪贴板' if ok else '复制失败,请手动复制'}: {cmd}",
                style="bold green" if ok else "yellow",
            )
            self._refresh_settings_pane()

        elif bid == "mcp-register":
            name = self.query_one("#mcp-name-input", Input).value.strip() or "twitter-dm"
            self.config.mcp_server_name = name
            self.config.save()
            cmd = self._build_mcp_command()
            self.log_line(f"尝试执行: {cmd}", style="bold cyan")
            self._register_mcp(cmd)
            self._refresh_settings_pane()

        elif bid == "export-dir-save":
            value = self.query_one("#export-dir-input", Input).value.strip()
            self.config.export_dir = value
            self.config.save()
            self.log_line(
                f"导出目录已保存: {value or '(工作目录)'}", style="bold green"
            )
            self._refresh_settings_pane()

        elif bid == "export-all":
            self.action_export("all")
        elif bid == "export-current":
            self.action_export("current")
        elif bid == "export-changes":
            self.action_export("changes")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 在输入框里按回车也保存对应设置
        if event.input.id == "proxy-input":
            self.config.proxy = event.value.strip()
            self.config.save()
            self.log_line(f"代理已保存: {self.config.proxy or '(空)'}", style="bold green")
            self._refresh_settings_pane()
        elif event.input.id == "interval-input":
            try:
                self.config.auto_refresh_minutes = max(0, int(event.value.strip() or "0"))
                self.config.save()
                self._apply_interval_timer()
                self._refresh_settings_pane()
            except ValueError:
                self.notify("间隔必须是整数分钟。", severity="error")
        elif event.input.id == "mcp-name-input":
            self.config.mcp_server_name = event.value.strip() or "twitter-dm"
            self.config.save()
            self._refresh_settings_pane()
        elif event.input.id == "export-dir-input":
            self.config.export_dir = event.value.strip()
            self.config.save()
            self.log_line(
                f"导出目录已保存: {self.config.export_dir or '(工作目录)'}",
                style="bold green",
            )
            self._refresh_settings_pane()

    # ----- 其它快捷键 -----

    def action_toggle_filter(self) -> None:
        self.show_only_changes = not self.show_only_changes
        self._reload_data()
        self.log_line(
            f"切换显示模式: {'只看变化' if self.show_only_changes else '全部展示(含未变)'}",
            style="bold magenta",
        )

    def action_toggle_headed(self) -> None:
        if self.is_refreshing:
            self.notify("刷新中,等结束再切换。", severity="warning")
            return
        self.headed_scrape = not self.headed_scrape
        self.log_line(
            f"刷新模式切换为: {'可见浏览器' if self.headed_scrape else 'headless'}",
            style="bold magenta",
        )

    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = f"tab-{name}"

    def action_open_selected(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        if not active:
            return
        src = active.replace("tab-", "")
        if src == "settings":
            return
        try:
            pane = self.query_one(f"#pane-{src}", SourcePane)
            if pane.open_selected():
                self.log_line("已在默认浏览器打开当前会话。")
            else:
                self.log_line("当前未选中任何会话。", style="yellow")
        except Exception as e:
            self.log_line(f"打开失败: {e}", style="red")

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.log_line("日志已清空。")

    # ----- 导出 -----

    def action_export(self, scope: str = "all") -> None:
        """导出当前数据为带时间戳的 JSON 文件。

        scope:
            "all"      = 导出全部三个源(等同于复制 dm_check.json)
            "current"  = 仅导出当前激活 tab 的源
            "changes"  = 仅导出 new + updated(各源都过滤,丢弃 unchanged)
        """
        if not self.data or not self.data.get("sources"):
            self.notify("没有可导出的数据,先按 r 刷新。", severity="warning")
            return

        sources = dict(self.data.get("sources") or {})

        # 按 scope 过滤
        if scope == "current":
            active = self.query_one("#tabs", TabbedContent).active
            if not active or active == "tab-settings":
                self.notify("当前在设置页,请先切到聊天/优先/隐藏。", severity="warning")
                return
            src_name = active.replace("tab-", "")
            sources = {src_name: sources.get(src_name) or {}}
        elif scope == "changes":
            filtered: dict = {}
            for k, v in sources.items():
                v = v or {}
                filtered[k] = {
                    "new": v.get("new") or [],
                    "updated": v.get("updated") or [],
                    "unchanged": [],
                    "unchanged_count": v.get("unchanged_count", 0),
                    "total_scanned": v.get("total_scanned", 0),
                }
            sources = filtered

        payload = {
            "exported_at": datetime.now().astimezone().isoformat(),
            "scope": scope,
            "source_json": str(self.json_path),
            "fetched_at": self.data.get("fetched_at"),
            "self_user_id": self.data.get("self_user_id"),
            "sources": sources,
        }

        # 决定输出目录
        target_dir = Path(self.config.export_dir).expanduser() if self.config.export_dir else Path.cwd()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log_line(f"创建目录失败: {e}", style="bold red")
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = scope if scope == "all" else f"{scope}"
        out_path = target_dir / f"dm_export_{suffix}_{ts}.json"
        try:
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            self.log_line(f"写入失败: {e}", style="bold red")
            return

        size = out_path.stat().st_size
        counts = " ".join(
            f"{k}={len((v or {}).get('new', [])) + len((v or {}).get('updated', [])) + len((v or {}).get('unchanged', []))}"
            for k, v in sources.items()
        )
        msg = f"导出成功 [{scope}] -> {out_path}  ({size:,} 字节,{counts})"
        self.log_line(msg, style="bold green")
        try:
            status = self.query_one("#export-status", Static)
            status.update(msg)
        except Exception:
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DM 数据可视化 TUI。")
    p.add_argument("--json", default="dm_check.json", help="dm_check JSON 路径")
    p.add_argument("--refresh-on-start", action="store_true",
                   help="启动时先跑一次 dm_check.py 再进 TUI")
    p.add_argument("--headless-scrape", action="store_true",
                   help="刷新时用 headless 浏览器 (默认 headed; X 经常拦 headless)")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    opts = parse_args(sys.argv[1:])
    DMViewer(
        Path(opts.json),
        refresh_on_start=opts.refresh_on_start,
        headed=not opts.headless_scrape,
    ).run()
