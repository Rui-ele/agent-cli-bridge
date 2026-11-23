# 把 `claude -p` 当作委托 worker（2026-11-23，MacBook Pro）

为路线图第 4 步（Claude 驱动）做的探测。发起方：CC（主对话），用 [probe.py](probe.py) 直接调用 `claude`，没有经过 cc-delegate。Claude Code 2.1.330，模型 `haiku`（实际是 `claude-haiku-5-5`），每次调用约 0.003–0.006 美元。测试仓库建在 `~/.cc-delegate-probe-*` 临时目录里，探测结束后删除。

## 已核实

- **输出**：`--output-format stream-json --verbose` 逐行输出事件，类型有 `system/init`、`assistant`、`user`（工具结果）、`system/permission_denied`、`system/thinking_tokens`、`rate_limit_event`，最后一行是 `result/success`。`result` 事件里有 `session_id`、`structured_output`、`usage`（含缓存读写 token）、`modelUsage`（实际用的模型）、`total_cost_usd`、`permission_denials`、`num_turns`。
- **结构化回复**：`--json-schema '<JSON 字符串>'` 生效。模型通过一个名为 `StructuredOutput` 的工具交回结果，结果出现在 `result.structured_output`。
- **续接**：`--resume <session_id>` 接上的是同一个会话，`session_id` 不变，也记得上一轮做了什么。
- **只读**：`--permission-mode dontAsk --permission-prompts none --allowedTools Read Grep Glob Bash`，外加 Seatbelt 写入沙箱。Write 工具被权限模式拒绝；在 shell 里 `touch` 被沙箱拒绝（`Operation not permitted`）。仓库里没有生成任何文件。
- **写入**：`--permission-mode acceptEdits --allowedTools Bash` 不行。Claude 自己的规则把复合命令判为需要审批，`-p` 下没人审批，于是拒绝，git 提交没有做成。改用 `--permission-mode bypassPermissions` 加 Seatbelt 写入沙箱后，建文件、`git add`、`git commit` 都成功。往仓库外 `touch`、往 `~/.claude/` 下 `touch` 都被沙箱拒绝。
- **Seatbelt 需要放开的写入路径**（少了任何一项，Claude 都会报 EPERM）：
  - `~/.claude` 下 Claude 自己的状态目录（`projects`、`sessions`、`session-env`、`shell-snapshots`、`state`、`cache`、`file-history`、`todos`、`plans`、`debug` 等）；
  - `~/.claude.json` 及其同名的锁文件和备份文件；
  - `/private/tmp/claude-<uid>/`，Bash 工具的工作区；
  - `/private/tmp/claude-<hex>-cwd`，Bash 工具记录当前目录用的文件；
  - 系统临时目录和 `/dev`。

  `~/.claude` 的其余部分（settings、skills、hooks、tools、其他委托运行的目录）保持只读，以免 worker 改动宿主 CC 的配置。按这个清单跑完，调试日志里没有任何 EPERM。
- **环境变量**：从 CC 会话里启动时，父进程带着几十个 `CLAUDECODE`、`CLAUDE_CODE_*` 变量（会话 ID、消息 socket、`CLAUDE_CODE_SIMPLE=1` 等）。探测时把它们都清掉了，`claude -p` 照常登录、运行。没有逐个测试保留哪些变量会出问题。

## 第一轮踩到的坑

- 测试仓库一开始建在系统临时目录下。临时目录本身是可写的，所以「往外写」看起来成功了，其实测试是无效的。之后改把测试目录建在家目录下。
- 一开始把整个 `~/.claude` 设为可写，Bash 也因为 `/private/tmp/claude-<uid>` 不可写而全部失败。上面那份清单就是这样一步步确定下来的。

## 未核实

- SIGINT 能否让 `claude -p` 干净地结束它启动的子命令（留到驱动实测时做）。
- 长任务、子代理、并发、MCP 服务对这份写入清单的影响。
- 不同模型（sonnet、opus）的结构化输出表现；这次只测了 haiku。
