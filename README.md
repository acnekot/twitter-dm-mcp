# Twitter DM MCP(Playwright + XChat DOM)

> [English version](README.en.md)

通过 Playwright 驱动真实 Chromium + 持久化 profile,从 Claude Code(或任何 MCP host)读取和发送 Twitter/X 私信。不走官方 API,不依赖 `twikit` 等已失效的反向库。

附带一个中文 TUI(`dm_tui.py`),用于增量查看新 / 更新的 DM 和消息请求;另外提供独立 CLI 脚本做一次性抓取以及给 AI 总结用的 JSON 导出。

## 为什么需要真实浏览器

X 把 DM 重写成了 **XChat**,一个 Kotlin Multiplatform 应用。会话状态存在 IndexedDB 里,事件通过 Thrift 二进制编码后由 GraphQL 同步——也就是说不再有一个完整的 `inbox_initial_state.json` 给你拦截了。Thrift schema 没法不破解逆向,所以**驱动渲染后的 UI** 是目前唯一可靠的路:

- **收件箱 + 消息请求**:从 `[data-testid^="dm-conversation-item-"]` / `dm-message-request-item-` 抓
- **会话历史**:从 `[data-testid^="message-text-"]` 抓(气泡的水平对齐用来判断"我发的"还是"对方发的")
- **发送**:写入 `[data-testid="dm-composer-textarea"]` 然后按 Enter 提交

代价是抓取期间需要保留一个有头 Chromium 进程(空闲约 300 MB 内存)。X 几乎总是会拦截 headless 模式,所以所有脚本默认都带 `--headed`。

## 安装

```powershell
pip install -r requirements.txt
playwright install chromium
```

需要 Python 3.11+。

## 首次登录

持久化 profile 位于 `~/.twitter-dm-mcp/browser-data`(可通过 `TWITTER_PROFILE` 覆盖)。一次性设置:

```powershell
$env:TWITTER_PROXY = "http://127.0.0.1:7897"   # 仅当 x.com 在你本地被墙
python login.py
```

Chromium 会弹出 `https://x.com/home`,你在窗口里手动完成登录;脚本检测到 `auth_token` cookie 出现后会自动退出。之后所有脚本都复用这个 profile,不用重复登录。

如果脚本报 `Found a stale auth_token cookie - x.com bounced us to login`,说明 cookie 过期了,在窗口里重新登一次即可。

## 项目结构

| 文件 | 用途 |
|---|---|
| `server.py` | FastMCP 服务器(stdio),暴露 5 个工具 |
| `login.py` | 交互式首次登录 |
| `dm_check.py` | 增量抓取:每个源最近 50 条(聊天 / 优先 / 隐藏),与 `~/.twitter-dm-mcp/dm_state.json` diff,产物 `dm_check.json` |
| `dm_tui.py` | Textual TUI 中文界面:标签视图 + 设置页 + 实时日志 |
| `read_inbox.py` | 独立脚本:列出主收件箱会话 |
| `read_history.py` | 独立脚本:打印某个会话的消息历史 |
| `read_requests.py` | 独立脚本:抓 Priority / Hidden 请求 |
| `send_test.py` | 独立脚本:dry-run 或真实发送 DM |
| `export_requests.py` | 一键导出两个 Requests tab 的完整内容 |

## MCP 工具

把 server 挂到 Claude Code:

```powershell
claude mcp add twitter-dm `
  --env HEADLESS=1 `
  --env TWITTER_PROXY=http://127.0.0.1:7897 `
  -- python "C:\path\to\twitter-dm-mcp\server.py"
```

TUI 的 **设置 → MCP 集成** 区域可以自动生成并复制这条命令。

| 工具 | 说明 |
|---|---|
| `list_dm_conversations(max_count=20)` | 从 `/i/chat` 抓主收件箱会话(名称、相对时间、最后预览、conv_id、对方 user_id) |
| `read_dm_history(username="", count=50, conversation_id="")` | 打开一个会话,返回渲染后的消息列表(`mine`/`them` 通过气泡对齐推断) |
| `send_dm(username="", text="", conversation_id="")` | 把文本写入 XChat composer,按 Enter 发送 |
| `list_message_requests(tab="priority", max_count=50)` | `/i/chat/requests` 的未接受请求。`tab` 取 `priority` 或 `hidden` |
| `get_my_info()` | 当前登录账号的 id + screen_name(从 cookie + Viewer API 拿) |

所有工具返回 JSON 字符串:`{"ok": true, "data": ...}` 或 `{"ok": false, "error": "..."}`。绝不会抛异常出来。

## TUI 工作流

```powershell
python dm_tui.py
```

四个 tab — **聊天 / 优先 / 隐藏 / 设置** — 加底部实时日志面板。

快捷键:

| 键 | 行为 |
|---|---|
| `r` | 刷新(后台 subprocess 跑 `dm_check.py`,stderr 实时打到日志面板) |
| `e` | 导出当前状态为带时间戳的 JSON |
| `f` | 切换显示模式:只看变化 / 显示全部 50 条 |
| `h` | 切换 headed/headless 抓取模式(默认 headed,X 拦截 headless) |
| `1` / `2` / `3` / `4` | 跳转 聊天 / 优先 / 隐藏 / 设置 |
| `↑` / `↓` | 移动选中 |
| `Enter` | 在默认浏览器打开当前选中的会话 |
| `Ctrl+L` | 清空日志面板 |
| `q` | 退出 |

**设置页**配置持久化在 `~/.twitter-dm-mcp/tui_config.json`:

- **代理** — 自动以 `TWITTER_PROXY` env 注入到每一个子进程
- **登录** — 启动 `login.py`,过程日志实时显示
- **定时刷新** — 每 N 分钟自动刷新,基于 Textual 的 `set_interval`
- **MCP 集成** — 生成并复制 `claude mcp add` 命令(也可以直接执行注册)
- **导出** — 输出目录 + 三种模式:全部 / 仅当前 tab / 仅变化

## 独立脚本

每个脚本都能独立运行,共用同一套环境变量。

```powershell
# 收件箱
python read_inbox.py 20 --headed

# 某个会话的历史
python read_history.py "1441009715782115342:1552213521253150720" 30 --headed

# 消息请求
python read_requests.py priority --headed
python read_requests.py hidden 500 --raw --headed > hidden.json

# 发送(先 dry-run)
python send_test.py "<conv_id>" "测试" --headed                # dry-run,不按 Enter
python send_test.py "<conv_id>" "测试" --send --headed         # 真实发送

# 一次性把两个 Requests tab 完整导出到单个 JSON
python export_requests.py --hidden-max 800 --headed
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HEADLESS` | `1` | `0` 显示浏览器。首次登录必须 `0`。带 `--headed` 参数的脚本会覆盖这个 env。 |
| `TWITTER_PROXY` | (无) | 例 `http://127.0.0.1:7897`。传给 Playwright 的 `proxy.server`。TUI 设置页里也能写。 |
| `TWITTER_PROFILE` | `~/.twitter-dm-mcp/browser-data` | 覆盖持久化 user-data-dir 的位置 |

## 运行时产物(已 gitignore)

| 路径 | 内容 |
|---|---|
| `~/.twitter-dm-mcp/browser-data/` | Chromium 持久化 profile(cookies、IndexedDB 等) |
| `~/.twitter-dm-mcp/dm_state.json` | 每个源上次见过的 `{conv_id: preview}` 映射,用于增量 diff |
| `~/.twitter-dm-mcp/tui_config.json` | TUI 设置(代理、定时、MCP 服务名、导出目录) |
| `./dm_check.json` | `dm_check.py` 上次运行的输出 |
| `./dm_export_*_<时间戳>.json` | TUI 按 `e` 或设置页按钮导出的快照 |

## 已知限制

- **必须 headed**。X 在 XChat 页面对 headless 模式拦截非常稳定 —— `goto /i/chat/requests` 会 30 秒超时或返回空壳页面。所有脚本默认 `--headed`。
- **拿不到消息的精确 Unix 时间戳**。XChat 只渲染相对时间(`13:35`、`4月23日周四`、`1w`),Unix 时间藏在 Thrift 编码的 GraphQL 响应里,我们没办法不解 schema 就解码。
- **拿不到单条消息的 sender_id**。方向(`mine`/`them`)通过气泡水平对齐推断(我发的是 `bg-primary` 右对齐,对方是 `bg-gray-50` 左对齐)。纯媒体消息标记为 `mine=null`、`kind="media"`。
- **Hidden Requests 通常抓到 440~510 条,即使 X 在小红点里显示 548**。部分项可能已过期 / 隐藏在更深的滚动位置。调大 `--hidden-max-wait` 可以多抓一些。
- **高频抓取会触发账号风控**。定时刷新 5 分钟一般没事,亚分钟级别就是在自找麻烦。
- **UI test-id 会变**。XChat 在持续迭代,如果某个选择器失效(最可能是 composer 或消息气泡结构),修改 `server.py` 顶部那几个常量即可。

## 许可证

MIT。如果根目录有 `LICENSE` 文件则以其为准,否则按 MIT 处理。
