# Agent CLI Bridge

Claude Code（CC）和 Codex 都可以把代码审阅、调研或修改任务交给本机的 agent CLI，再收回结果。两个发起方共用标准库脚本 [cc_delegate.py](baseline/cc-delegate/cc_delegate.py)。Codex 调 Claude Code 时，单独的 worker 使用 Anthropic 官方 Python Agent SDK。

从 CC 发起的委托已在两台 Mac 上实测，接单方包括 Codex、另一个 Claude Code 和 Antigravity CLI（agy）。Codex 发起时加 `--caller codex`，可调 Claude Code 或 agy。agy 发起的委托还有待实现。

## 让 Claude Code 帮你装

在 Mac 上打开 Claude Code，把下面这段话发给它，它会按 [INSTALL.md](INSTALL.md) 一步步装好，需要你点确认、输密码或登录账号时会停下来告诉你：

```text
请读 https://raw.githubusercontent.com/Rui-ele/agent-cli-bridge/main/INSTALL.md ，按里面的步骤帮我在这台 Mac 上把 cc-delegate 装好。需要我本人操作的地方，停下来告诉我怎么做。
```

想自己动手装，看下面的「前提」和「安装」。

## 前提

- **macOS**。脚本用到 `fcntl`，原生 Windows 跑不了。`--to claude` 和 agy 的 ACP 后端靠 macOS 自带的 `/usr/bin/sandbox-exec` 限制写入，Linux 上会以 `os_sandbox_unavailable` 拒绝；Linux 上只能用 `--to codex` 和 `--to agy --backend exec`。
- **Python 3.10 或更高**。macOS 自带的 `/usr/bin/python3` 是 3.9，跑不了（脚本会提示版本不够并退出）。没有开发工具的 Mac，最简单的是从 [python.org](https://www.python.org/downloads/macos/) 下载安装包；装过 Homebrew 的可以 `brew install python`。装完新开一个终端，`python3 --version` 应显示 3.10 或更高。
- **git**。默认写入模式要求仓库有至少一个提交。普通文件夹使用下文的 `--workspace files`。
- **至少一个接单方的 CLI，已装好并登录**：
  - Codex：`codex`，安装见 [Codex CLI 文档](https://developers.openai.com/codex/cli)；
  - agy：`agy`，安装见 [Antigravity CLI 安装说明](https://antigravity.google/docs/cli/install)；
  - Claude Code：`claude`，安装见 [Claude Code 安装说明](https://code.claude.com/docs/en/setup)。Claude 桌面版自带的 `claude` 不在 PATH 上，终端里找不到，要另外装一次 Claude Code CLI。

  装好后在新终端里运行 `codex --version`、`agy --version` 或 `claude --version` 确认能找到。找不到时，脚本会在开始前报 `… executable not found in PATH`。

  通过 SSH 在另一台 Mac 上使用时，macOS 钥匙串是锁着的，存在钥匙串里的登录读不到：claude 可以用 `claude setup-token` 生成长期令牌，放进环境变量 `CLAUDE_CODE_OAUTH_TOKEN`；agy 在 SSH 会话里登录一次即可。
- **模型名**要填你的账号在该 CLI 里能用的。下文示例用的是作者的模型，可以用各 CLI 列模型的命令（如 `agy models`）查看。

## 安装

```sh
git clone https://github.com/Rui-ele/agent-cli-bridge.git
mkdir -p ~/.claude/tools
cp -R agent-cli-bridge/baseline/cc-delegate ~/.claude/tools/cc-delegate
python3 ~/.claude/tools/cc-delegate/cc_delegate.py --help
```

可以先跑离线测试，确认环境没问题（不联网、不调用真实的 CLI）：

```sh
cd ~/.claude/tools/cc-delegate && python3 -m unittest discover -s tests
```

**让 CC 自己会用（可选）**：把技能模板复制到 CC 的技能目录，CC 就会在合适的时候自动委托：

```sh
mkdir -p ~/.claude/skills/delegate
cp ~/.claude/tools/cc-delegate/skill/SKILL.md ~/.claude/skills/delegate/SKILL.md
```

模板里没有作者的模型分工，开头留了一节「我的分工」，按你自己的订阅和习惯填。

## 试一次只读委托

在任意一个 git 仓库里运行。`--to` 可选 `codex`、`claude` 或 `agy`，`--model` 换成你的账号可用的模型：

```sh
python3 ~/.claude/tools/cc-delegate/cc_delegate.py run \
  --to codex --model gpt-6.2-astra \
  --mode ro --project-dir . \
  --task '读 README，用三句话说明这个项目是做什么的。'
```

成功时，终端最后几行是回执，其中有 `status="ok"` 和几条 `summary`。完整报告在回执里 `report_path` 指向的文件。

### 从 Codex 发起

在仓库中可直接运行，例如让 agy 通过 ACP 审阅 README：

```sh
python3 baseline/cc-delegate/cc_delegate.py run --caller codex \
  --to agy --backend acp --model gemini-pro-orchestrator-neon \
  --mode ro --project-dir . --no-fallback --task '读 README，指出最需要澄清的一处。'
```

调 Claude Code 时将接单参数换成 `--to claude --model haiku`（省略 `--backend acp`）。写入、续接、状态、取消和验收沿用同一套命令。回执和记账中的 `caller` 标明发起方，`--resume` 接的是接单方的会话。

首次使用 SDK 后端时准备一个专用 Python 环境，启动委托的进程需要能看到环境变量：

```sh
python3 -m venv ~/.local/share/cc-delegate/sdk-venv
~/.local/share/cc-delegate/sdk-venv/bin/python -m pip install 'claude-agent-sdk==0.2.187'
export CC_DELEGATE_SDK_PYTHON="$HOME/.local/share/cc-delegate/sdk-venv/bin/python"
```

`CC_DELEGATE_SDK_PYTHON` 的相对路径从发起命令的目录解析。脚本在创建克隆前检查该解释器能否加载 SDK。

SDK 复用本机已登录的 `claude`。`run` 在启动时输出 `task_id`。每个任务持有一个 `ClaudeSDKClient`，可以从另一个命令读取进度、发送消息和处理权限请求。需要由发起方处理工具审批时加 `--approval ask`，用 `status` 查看待审批工具、输入和请求 ID，再用 `approve` 或 `deny` 回答。写入继续受独立克隆和 macOS Seatbelt 约束。

要在看过第一轮结果后再决定追问什么，给 `run` 加 `--keep-open`。当 `status` 显示 `state="idle"` 和本轮摘要时，可以用 `queue` 开下一轮，也可以用 `finish` 交付。期间客户端、任务名额和写入克隆持续保留。每轮结果记在任务目录的 `events.jsonl` 中，最终报告取最后一轮，分支交付包含整个任务的提交。中断会停止当前轮，已执行的工具操作仍然保留。结束后再接着改，用 `--resume <worker_session_id>` 恢复对话，并用原目标分支 `--onto` 或上次结果 `--base` 选择代码起点。

SDK 回执的 token 用量按本次任务内的各轮累加，`cost_usd` 来自 SDK 的累计值，续接时可能含以前轮次。运行中追加、排队、中断的实测见[实验记录](experiments/2026-11-24-claude-agent-sdk/README.md)。

Codex 默认只发送任务和显式指定的文件摘录。要带当前对话，加 `--context scoped`（最近三轮）或 `session`（对话和裁剪过的 shell 记录）。`full` 保留这些 shell 命令的输出，仍受上下文预算和脱敏规则限制。脚本按当前 `CODEX_THREAD_ID` 定位会话，也可用 `--codex-session FILE` 显式指定 rollout。支持的记录是用户消息、助手消息和已完成的 shell 命令；旧格式只有对话消息时只带消息。续接时沿用 worker 的历史。

## 暂存普通文件夹的修改

假设要整理一份笔记，可以让接单方先修改副本。脚本默认不写回原文件夹，也不会在里面创建 `.git`。

```sh
delegate=~/.claude/tools/cc-delegate/cc_delegate.py
python3 "$delegate" run --to codex --model gpt-6.2-astra \
  --mode write --workspace files --project-dir ~/notes \
  --task '统一笔记标题格式，保留正文。'
```

用回执里的任务号替换下面的 `TASK_ID`。回执列出新增、删除和修改的相对路径，`report_path` 指向包含文本 diff 的报告。二进制或超过 1 MiB 的文件列出大小和 SHA-256。

```sh
python3 "$delegate" status TASK_ID
python3 "$delegate" apply TASK_ID
python3 "$delegate" rollback TASK_ID
```

`status` 的 `workspace_phase` 显示交付阶段。任务成功或部分完成时，有文件改动就进入 `pending_apply`，没有改动就进入 `no_changes`。失败或取消的任务进入 `failed`，即使接单方已改动副本。`no_changes` 和 `failed` 都会拒绝 `apply`，并按默认七天规则清理。

`apply` 在目录锁内核对全部受影响路径，发现冲突会列出路径并整批停止。通过后先备份原件，再逐项删除或用同目录临时文件原子替换，完成后显示 `applied`。`rollback` 同样先检查全部路径，遇到写回后的用户编辑就停止，回退完成后显示 `rolled_back`。

原子替换按文件生效。如果写回期间其他应用又修改或锁住文件，前面完成的项目会保留。解除占用、解决冲突后，可重跑同一条 `apply` 或 `rollback` 命令，脚本会根据进度日志继续。原子替换可能丢失 macOS 扩展属性和 Finder 标签。

每份任务的副本、基线清单、差异、备份和进度日志都在私有运行目录中。同卷且支持 APFS 克隆时使用写时复制，其他情况按字节复制。同一个目标目录同时只允许一个写入任务。待写回的工作区会被 `prune` 保留。决定放弃时运行 `mark TASK_ID --outcome discarded`。已写回、已回退或已放弃的工作区可以按默认七天规则清理，清理备份后即无法再用该任务回退。

第一版按整目录处理普通文件，不接收 `--include`、`--base`、`--onto` 或 `--resume`。Git 仓库及其子目录沿用默认分支交付。空目录和目录权限不单独交付。符号链接、硬链接、特殊文件、嵌套的 `.git` 和命中敏感路径规则的文件会使任务停止。为兼容不同文件系统，路径须使用 UTF-8，不支持仅大小写或 Unicode 规范形式不同的异名，以及文件与目录互换。默认文件数和目录数各最多 10,000，总大小最多 1 GiB，可通过 `CC_DELEGATE_FILES_MAX_COUNT`、`CC_DELEGATE_FILES_MAX_BYTES` 调整。

普通文件夹容易混有私人资料，委托前请核对整个目录的内容。沙箱只限制写入，接单方仍能读取工作区以外的文件。Codex 使用原生工作区沙箱。agy 和 Claude 的两种后端都要求 macOS Seatbelt，继承调用方的 `HOME` 和 XDG 环境，以便找到已有登录状态。agy 可写 `~/.gemini`，Claude 可写其常用状态目录、当前工作区对应的 `projects` 子目录和 `~/.claude.json` 及其原子更新文件。两者的项目改动和临时文件分别写入任务的 `work/` 与 `scratch/`，原目录、基线和回执受写保护。

## 默认行为和边界

请在用于工作材料之前读完这一节。

- 同一系统用户下的其他会话也能凭 `task_id` 控制任务。
- `CC_DELEGATE_SDK_PYTHON` 指向的解释器以宿主权限运行。请指向你自己创建并信任的环境，避免使用待审项目里的 `.venv`。
- **发出去的上下文**：默认 `--context none`，只发任务文本和固定规则。`--include FILE` 只附上文件的路径和开头几行，接单方需要时会自己去读全文。由 CC 发起时，显式用 `--context scoped|session|full` 可带上 CC 对话，默认选该项目最新的 CC 会话。由 Codex 发起时，按上节选取 Codex 会话。两者打包前都会脱敏常见的密钥格式，普通业务内容会保留。
- **只读和写入**：`--mode ro` 限制接单方修改项目文件。Claude 需要在 `~/.claude/projects/<项目>/` 写入自己的会话记录，因此只读任务也能改动该目录下其他 CC 会话的记录。默认的 Git 写入模式在项目旁边的 `../.wt-delegate/<task_id>` 建一个独立克隆，接单方只在克隆里改，你的工作区和分支不会被直接改动。结束后，脚本把提交取回主仓库的 `delegate/<task_id>` 分支；加了 `--onto <已有功能分支>` 时，还会把该分支快进到这个提交。不能是快进、该分支在别的 worktree 里检出、或者 `main`/`master` 时，脚本拒绝移动分支。回执里给出 `rollback` 回退命令。
- **并发名额**。同一仓库默认允许 4 个写入任务同时运行，只读任务默认不限。名额满时，`run` 会输出任务号和 `status="queued"`。排队中可用 `status` 查看、用 `cancel` 取消。拿到名额后才创建克隆或工作区并启动接单方。默认最多等 30 分钟，超时返回 `repository_readers_busy` 或 `repository_writers_busy`。目标分支已被另一个 `--onto` 任务锁定时立即返回 `onto_branch_busy`。`--workspace files` 的同目录写入冲突仍立即返回 `files_workspace_busy`。
- **Claude 的登录过期后，沙箱里刷新不了**：`--to claude` 在沙箱里运行，过期的登录令牌没法自动刷新，委托会报 401（`OAuth access token has expired`）。在终端里直接运行一次 `claude -p "say ok"` 刷新登录，再重新委托即可。原因推测是刷新要写钥匙串，被沙箱挡住了，未核实。
- **沙箱只管写，不管读和联网**：接单方能读到你账号能读的所有文件，也能联网。含密钥或凭据的仓库，建议只交给你信任的接单方，或者不要委托。
- **失败改派默认关闭**：agy 失败或卡死时，可以自动改派给 Codex 重跑，用 `--fallback` 或环境变量 `CC_DELEGATE_AGY_FALLBACK=1` 开启。开启后材料会发给另一家，也会多花一份额度。
- **委托深度**：worker 里再往下委托最多两层（`CC_DELEGATE_MAX_DEPTH`）。
- **本机会留下的东西**：
  - `~/.claude/delegate-runs/<task_id>/`：发出去的完整任务包、事件流、回执和报告；
  - `~/.claude/delegate-log.jsonl`：记账；
  - Git 写入任务另有 `../.wt-delegate/` 下的克隆，以及主仓库里的 `delegate/<task_id>` 分支。

  用 `prune --dry-run` 查看可以清理的内容，确认后再去掉 `--dry-run`。默认只清结束超过 7 天的；有未提交改动、或有未合并提交的克隆和分支会保留。索引含 `assume-unchanged` 或 `skip-worktree` 条目时也会保留克隆，因为 Git 可能跳过这些文件的改动。宿主检查克隆和取回提交时，每条 Git 命令默认限时 60 秒，可用 `CC_DELEGATE_GIT_TIMEOUT` 设置正整数秒数。检查超时会保留克隆。有写入任务在跑时不要清理。

  SDK 监督进程异常退出时，worker 会断开会话。新建的 SDK 任务在两端都退出后可以按最后活动时间清理，仍按相同规则保留克隆中的工作。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `run …` | 发起委托（参数见 `run --help`） |
| `run … --shadow` | 发起影子任务，跳过仓库名额并关闭失败改派。继续遵守 `--onto` 分支锁和普通文件夹的写入锁，任务元数据和记账标记 `shadow: true` |
| `status <task_id>` | 查看排队状态、运行中任务的最近命令和消息，或已完成任务的回执 |
| `cancel <task_id>` | 取消任务 |
| `steer <task_id> --message '…'` | Codex daemon 中途插话；Claude SDK 立即提交一条消息，由 Claude 决定在哪一轮采纳 |
| `queue <task_id> --message '…'` | Claude SDK 等当前轮返回结果，再在同一会话发起下一轮 |
| `finish <task_id>` | 结束空闲的 `--keep-open` 任务，生成最终报告，按原参数交付分支 |
| `interrupt <task_id> [--message '…']` | Claude SDK 打断当前轮；附带消息时在同一会话继续 |
| `approve\|deny <task_id> --request-id ID` | 对一个具体的 Claude SDK 工具审批请求作出一次决定 |
| `mark <task_id> --outcome accepted\|edited\|discarded` | 记下结果有没有用上，用于以后调整分工 |
| `prune [--older-than DAYS] [--dry-run]` | 清理旧的运行目录、克隆和已合并的分支 |

## 后端和环境变量

| 接单方 | 后端 | 说明 |
| --- | --- | --- |
| Codex | `exec`（默认）、`daemon` | `daemon` 连本机 Codex 守护进程，冷启动更快，支持 `steer`，任务会出现在 Codex 客户端的会话列表里；连不上时自动退回 `exec` |
| agy | `exec`（即 `agy -p`）、`acp` | 写入任务默认走 `acp`（外套 Seatbelt 沙箱，能正常 git 提交）；只读任务默认 `agy -p` |
| Claude | `sdk`、`print` | Codex 发起默认使用官方 SDK；CC 发起默认使用原有 print 驱动。两者都通过 Seatbelt 启动本机 Claude CLI |

| 环境变量 | 作用 |
| --- | --- |
| `CC_DELEGATE_CODEX_BACKEND`、`CC_DELEGATE_AGY_BACKEND` | 各接单方的默认后端 |
| `CC_DELEGATE_AGY_FALLBACK=1` | agy 失败时改派 Codex |
| `CC_DELEGATE_MAX_WRITERS` | 同一仓库的写入任务上限，默认 4，可设正整数 |
| `CC_DELEGATE_MAX_READERS` | 同一仓库的只读任务上限，设正整数才限制 |
| `CC_DELEGATE_SLOT_WAIT` | 名额满时的最长等待秒数，默认 1800，设 `0` 立即返回 busy |
| `CC_DELEGATE_MAX_DEPTH` | 委托深度上限（默认 2） |
| `CC_DELEGATE_HOME` | 运行目录和记账的位置（默认 `~/.claude`） |

## 更多

[开发约定](AGENTS.md) · [路线图和完成记录](docs/roadmap.md) · [实机实验](experiments/) · [调研笔记](research/) · [MIT 许可证](LICENSE)
