# Codex 各入口能否使用浏览器（2026-11-23，MacBook Pro）

codex-cli 0.171.0，模型 gpt-6.2-astra。每个入口都发同一个只读探测：调用一次 `cua.getState()`，不点击、不输入。

| 入口 | 运行在哪个进程 | 结果 |
| --- | --- | --- |
| `codex exec`，cc-delegate 参数（`workspace-write`，`approval_policy="never"`） | 独立进程 | 失败：`Browsers: Error: The browser access host is unavailable` |
| `codex exec`，用户默认配置（`on-request`，guardian 审批） | 独立进程 | 同上 |
| 交互式 `codex`（普通终端） | 连接受管守护进程 `codex app-server --listen unix:// --managed-daemon` | 同上 |
| ChatGPT 桌面 app 里的 Codex | app 自己启动的 `codex app-server`（stdio，父进程是 Codex Aperitif） | 用户日常可用，本次未重测 |

## 原因

浏览器工具 `node_repl` 要靠 `BROWSER_USE_ACCESS_PIPE_PATH`、`BROWSER_USE_ACCESS_EXECUTOR_ID`、`BROWSER_USE_ACCESS_VERIFYING_IDENTITY` 这三个环境变量连接 app 的浏览器服务。

- 只有 app 自己启动的 app-server 进程带这三个变量。受管守护进程不带，所以经守护进程运行的交互式 `codex` 也连不上。
- 在命令行里运行 `codex doctor`，也会报这三个变量未设置。
- app 的 app-server 只和 app 之间用 stdio 通信，没有监听可供外部连接的 socket 或端口。
- 与沙箱参数无关：用户默认配置下同样失败。
- 日志里另有一条本机 MCP 服务未运行导致的连接报错，和浏览器无关。

## 结论

CLI 的三个入口（exec、交互式、守护进程）都不能直接使用 app 的浏览器能力。原计划的第三步是「把委托改为经守护进程」，但守护进程本身就是交互式 `codex` 所用的那个进程，已经确认没有浏览器，所以没有执行这一步。

不打算采用的做法：伪造这三个环境变量（会绕过 app 的身份校验）；用操控电脑的方式去点 Codex app。

## 尚未验证的方向

- app 是否提供官方途径，让外部程序把任务送进 app 内的会话。
- 给 Codex 另接一个不依赖 app 的浏览器自动化 MCP（例如 Playwright 类），在 `exec` 下能否使用，能否沿用登录状态。
- 守护进程入口（`codex queue`、`app-server proxy`）对其他能力的价值，例如会话可在 TUI 中查看和接管。
