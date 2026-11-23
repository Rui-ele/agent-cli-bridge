# `agy acp` 实测：会话能力、权限与沙箱（2026-11-23，MacBook Pro）

agy 1.5.14，ACP schema v1（[agentclientprotocol/agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol) `schema/v1`）。探测脚本 `probe.py` 只用标准库，以 stdio JSON-RPC 与 `agy acp` 通信。

## 会话能提供什么

- `initialize` 返回 `loadSession: true`，`sessionCapabilities` 含 `resume`、`load`、`list`、`close`；认证方式有 Google 账号、Gemini Enterprise 和 API key（本机已登录，不需要另外调 `authenticate`）。
- `agy acp` 没有 `--model`、`--mode`、`--settings` 参数。这三样都要在会话里设置：
  - `session/new` 返回的 `configOptions` 里有 `model`（可选值如 `gemini-4-neon`、`gemini-4-argon`）、`thought_level`（`low|medium|high|xhigh`）和 `mode`。
  - 注意：模型名和 `agy -p` 的不一样。`-p` 的 `gemini-4-neon-high` 对应 ACP 的 `model=gemini-4-neon` 加上 `thought_level=high`。
  - `modes`：`default`（编辑和命令都要先请求许可）、`accept-edits`（工作区内的编辑自动批准）、`auto`、`plan`。
- 用 `session/set_config_option` 设置模型、思考级别和模式，都能生效。
- 一轮对话的用量在 `session/prompt` 的返回值里（`inputTokens`、`outputTokens`、`cachedReadTokens`、`thoughtTokens`、`totalTokens`）。

## 工具和权限怎么表现

- 读文件（`kind: read`）不会请求许可，直接执行。客户端声明了 `fs.readTextFile` 支持，agy 也不走 `fs/read_text_file`，仍然自己读。所以**客户端无法拦截读操作**。
- 执行命令（`kind: execute`）会发来 `session/request_permission`，选项只有 `allow`（allow_once）和 `deny`（reject_once）。客户端选了 deny，命令就标记为 failed，这一轮随之结束，而且没有最终回复。
- `accept-edits` 模式下，工作区内的编辑不会请求许可。

## 沙箱

| 方式 | 往工作目录之外 `touch` 一个文件 |
| --- | --- |
| `agy -p --sandbox`（cc-delegate 现有写模式） | 被挡住：命令显示完成，但文件没有生成 |
| `agy acp --sandbox`，客户端对命令选 allow | **成功写出**，沙箱没有生效 |
| `agy acp --sandbox --process-per-session` | **成功写出** |
| `agy acp`（不加 `--sandbox`），整个进程套在 `sandbox-exec` 里；profile 只允许写工作目录、`~/.gemini`、临时目录和 `/dev` | 被挡住；工作目录内写入正常，agy 登录和模型调用都正常 |

结论：在 ACP 下，agy 自带的 `--sandbox` 不起作用，写入隔离必须由我们自己在操作系统层面做（Seatbelt）。这一层是通用的，以后的 Claude 驱动也能用。

## 顺带发现（现有 `agy -p` 写模式）

同一个写任务在 `agy -p` 下没能提交：`printf … > probe.txt && git add … && git commit …` 这样的复合命令被 cc-delegate 给 agy 的权限设置拒绝了。这是现有行为，不是这次引入的，但说明 agy 写任务目前很难自己完成提交。
