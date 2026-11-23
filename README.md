# Agent CLI Bridge

在 Claude Code（CC）里，把一项代码审阅、调研或修改任务交给本机的 **Codex、另一个 Claude Code 或 Antigravity CLI（agy）**，再把结构化的结果拿回来。核心是一个只用 Python 标准库的脚本：[`baseline/cc-delegate/cc_delegate.py`](baseline/cc-delegate/cc_delegate.py)。

已在两台 Mac 上实测：MacBook Pro 和 Mac mini，都是由 Claude Code 发起委托。从 Codex 或 agy 反过来委托给 CC，目前还不支持。

## 前提

- **macOS**。脚本用到 `fcntl`，原生 Windows 跑不了。`--to claude` 和 agy 的 ACP 后端靠 macOS 自带的 `/usr/bin/sandbox-exec` 限制写入，Linux 上会以 `os_sandbox_unavailable` 拒绝；Linux 上只能用 `--to codex` 和 `--to agy --backend exec`。
- **Python 3.10 或更高**。macOS 自带的 `/usr/bin/python3` 可能是 3.9，跑不了；可以用 Homebrew 装：`brew install python`。
- **git**。写入任务要求项目是 git 仓库。
- **至少一个接单方的 CLI，已装好并登录**：
  - Codex：`codex`；
  - agy：`agy`；
  - Claude Code：`claude`。

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

## 默认行为和边界

请在用于工作材料之前读完这一节。

- **发出去的上下文**：默认 `--context none`，只发任务文本和固定规则。`--include FILE` 只附上文件的路径和开头几行，接单方需要时会自己去读全文。需要带上 CC 对话时，再显式用 `--context scoped|session|full`：脚本会读取 `~/.claude/projects/` 下该项目最新的会话记录，打包前会脱敏常见的密钥格式，但普通业务内容不会被去掉。
- **只读和写入**：`--mode ro` 不允许接单方改文件。`--mode write` 在项目旁边的 `../.wt-delegate/<task_id>` 建一个独立克隆，接单方只在克隆里改，你的工作区和分支不会被直接改动。结束后，脚本把提交取回主仓库的 `delegate/<task_id>` 分支；加了 `--onto <已有功能分支>` 时，还会把该分支快进到这个提交。不能是快进、该分支在别的 worktree 里检出、或者 `main`/`master` 时，脚本拒绝移动分支。回执里给出 `rollback` 回退命令。
- **沙箱只管写，不管读和联网**：接单方能读到你账号能读的所有文件，也能联网。含密钥或凭据的仓库，建议只交给你信任的接单方，或者不要委托。
- **失败改派默认关闭**：agy 失败或卡死时，可以自动改派给 Codex 重跑，用 `--fallback` 或环境变量 `CC_DELEGATE_AGY_FALLBACK=1` 开启。开启后材料会发给另一家，也会多花一份额度。
- **委托深度**：worker 里再往下委托最多两层（`CC_DELEGATE_MAX_DEPTH`）。
- **本机会留下的东西**：
  - `~/.claude/delegate-runs/<task_id>/`：发出去的完整任务包、事件流、回执和报告；
  - `~/.claude/delegate-log.jsonl`：记账；
  - 写入任务另有 `../.wt-delegate/` 下的克隆，以及主仓库里的 `delegate/<task_id>` 分支。

  用 `prune --dry-run` 查看可以清理的内容，确认后再去掉 `--dry-run`。默认只清结束超过 7 天的；有未提交改动、或有未合并提交的克隆和分支会保留。有写入任务在跑时不要清理。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `run …` | 发起委托（参数见 `run --help`） |
| `status <task_id>` | 查看正在跑的任务的最近命令和消息 |
| `cancel <task_id>` | 取消任务 |
| `steer <task_id> --message '…'` | 中途插话，仅 Codex 的 `--backend daemon` 支持 |
| `mark <task_id> --outcome accepted\|edited\|discarded` | 记下结果有没有用上，用于以后调整分工 |
| `prune [--older-than DAYS] [--dry-run]` | 清理旧的运行目录、克隆和已合并的分支 |

## 后端和环境变量

| 接单方 | 后端 | 说明 |
| --- | --- | --- |
| Codex | `exec`（默认）、`daemon` | `daemon` 连本机 Codex 守护进程，冷启动更快，支持 `steer`，任务会出现在 Codex 客户端的会话列表里；连不上时自动退回 `exec` |
| agy | `exec`（即 `agy -p`）、`acp` | 写入任务默认走 `acp`（外套 Seatbelt 沙箱，能正常 git 提交）；只读任务默认 `agy -p` |
| Claude | `print` | `claude -p`，外套 Seatbelt 沙箱 |

| 环境变量 | 作用 |
| --- | --- |
| `CC_DELEGATE_CODEX_BACKEND`、`CC_DELEGATE_AGY_BACKEND` | 各接单方的默认后端 |
| `CC_DELEGATE_AGY_FALLBACK=1` | agy 失败时改派 Codex |
| `CC_DELEGATE_MAX_WRITERS`、`CC_DELEGATE_MAX_READERS` | 同一仓库同时跑的写入、只读任务上限（默认 4、3） |
| `CC_DELEGATE_MAX_DEPTH` | 委托深度上限（默认 2） |
| `CC_DELEGATE_HOME` | 运行目录和记账的位置（默认 `~/.claude`） |

## 更多

[开发约定](AGENTS.md) · [路线图和完成记录](docs/roadmap.md) · [实机实验](experiments/) · [调研笔记](research/) · [MIT 许可证](LICENSE)
