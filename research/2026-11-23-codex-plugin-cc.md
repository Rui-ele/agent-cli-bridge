# openai/codex-plugin-cc 源码阅读（提交 db52e28，2026-07-07）

由 Neon（agy gemini-4-neon-high）只读阅读源码，CC 按行号抽查后改写。标「✓」的条目，CC 已在源码中核对过。路径相对于 `plugins/codex/`。

## 架构

```
斜杠命令 /codex:*（commands/*.md）
  → scripts/codex-companion.mjs（每条命令启动一次，跑完退出）
  → scripts/app-server-broker.mjs（每个仓库一个常驻进程，按需以 detached 方式启动）
      通过 unix:<tmpdir>/cxc-*/broker.sock 通信，协议是 JSONL JSON-RPC
  → codex app-server（由 broker 启动，走 stdio）
```

- broker 存在的理由有两个：复用已经启动的 app-server，省掉冷启动；让单独执行的 `/codex:cancel` 能对正在运行的任务插发 `turn/interrupt`。
- broker 一次只服务一个请求，忙时返回「Shared Codex broker is busy.」，只有 `turn/interrupt` 可以插队。这时调用方改为临时直连 app-server。✓（`scripts/app-server-broker.mjs:168-196`）
- Claude Code 触发 `SessionEnd` 时，hook 会关闭 broker，并结束本会话的任务。

## 协议用法

一次任务的调用顺序：`initialize`/`initialized` → `thread/start`（或 `thread/resume`）→ `turn/start`；代码审查改用 `review/start` → 处理 `turn/started`、`item/*`、`turn/completed` 事件 → 取消时发 `turn/interrupt`。没有用到 `turn/steer`。

## 安全

- 默认 `approvalPolicy: "never"`、`sandbox: "read-only"`；只有 `task --write` 时用 `workspace-write`。✓（`lib/codex.mjs:67-81`，`scripts/codex-companion.mjs:491`）
- app-server 发来的审批请求一律回 `-32601` 拒绝，不转给用户。✓（`lib/app-server.mjs:156-161`）
- 不做 worktree 隔离：`--write` 直接改主工作区。✓（整个仓库源码中没有出现 worktree）

## 后台任务

- 审查（review）在后台跑，靠的是 Claude Code 的后台 Bash；`task --background` 则以 detached 方式另起一个 worker 进程。
- 状态保存在 `${CLAUDE_PLUGIN_DATA}/state/<slug>-<hash>/`：`state.json` 是索引，每个任务另有 `jobs/<id>.json` 和 `jobs/<id>.log`。`status` 只读日志最后几行。
- 取消时先发 `turn/interrupt`，再结束整个进程树。
- 每次输出结果都附一条 `codex resume <thread-id>`，用户可以自己去 Codex 里接着做。

## 会话交接（/codex:transfer）

- 用 app-server 的 `externalAgentConfig/import`，把 Claude Code 的会话记录（`~/.claude/projects/**.jsonl`）导入成一个 Codex 会话，再给出 `codex resume`。✓（`lib/codex.mjs:724`）
- 只有 Claude Code → Codex 这一个方向。

## 结果回传

命令定义要求 CC 把 stdout 原样转给用户，不总结、不评论。✓（`commands/review.md:47-48`，`agents/codex-rescue.md:41-46`）这和我们「Codex 产出由 CC 改写后再给用户看」的规则相反。

## 弱点

没有隔离；`state.json` 没有文件锁；同一个仓库里开多个 Claude Code 会话时共用一个 broker，其中一个会话退出就会把 broker 关掉；普通 task 没有结构化回执。

## 对我们脚本的启示

值得照搬：
1. 改动小的 diff 直接放进提示词，改动大的只给文件清单，让 Codex 自己去读。
2. 回执里附上 `codex resume <id>`，方便用户接手。
3. 状态 JSON 和流式日志分开存，查进度时只读日志最后几行。
4. 用 `externalAgentConfig/import` 代替我们自己打包上下文。CC 的会话可以直接导入 Codex，这值得单独测一次（尚未验证）。

不适合我们：
1. 每个仓库只有一个、一次只服务一个请求的 broker，和我们的并发、隔离设计冲突。
2. 直接改主工作区，把输出原样整段塞回 CC。
3. 阻塞式的 Stop gate（停止前强制跑一次审查）：耗时长，还容易让两个 agent 来回循环。
