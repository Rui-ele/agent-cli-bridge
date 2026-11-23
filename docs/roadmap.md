# 路线图

2026-11-23 起，架构上暂告一段落：第 5–7 步等有实际需求再做。本文件从 README 里移出，保留设计取舍和完成记录。

## 分层架构

- **上层**：我们自己的、所有 agent 共用的部分，包括打包上下文、独立克隆隔离、并行锁、快进和回退、结构化回执、记账。
- **底层**：每家 agent 各自的「驱动」。
  - Codex 走 app-server，这是它的原生接口。
  - agy 走 `agy acp`。ACP 是它的原生接口，同时用来验证通用 ACP 驱动。
  - Claude 先用 `claude -p`，以后需要中途插话时，再换 Agent SDK 或 claude-agent-acp。
  - 其他支持 ACP 的 agent 走通用 ACP 驱动。

## 步骤

1. **收尾 Codex 驱动**
   - 第 2 步：加 `steer`、`cancel`、`status` 三个短命令。`steer` 发 `turn/steer`；`cancel` 先发 `turn/interrupt`，再结束进程；`status` 用 `thread/read`。
   - 第 3 步：会话改为非临时、按 task_id 命名，验证能否出现在 `codex agents` 里供用户接手；再测 `externalAgentConfig/import` 能否替代自己打包上下文。
2. **拆层重构**：把 `worker_command()` 里写死的 codex/agy 分支抽成统一的驱动接口，覆盖启动和续接、把事件翻译成统一格式、映射沙箱与权限、中断与插话。上层只认这个接口。现有的 Codex exec、Codex 守护进程、agy -p 先作为三个驱动挂上去，现有测试全部照常通过。
3. **通用 ACP 驱动**（只用标准库）：先让 agy 从 `-p` 换到 `agy acp`，对比两档沙箱和权限是否一致。
4. **Claude 驱动**（`--to claude`）：先用 `claude -p`。只读用 plan 模式或限定工具；写入在独立克隆里用 acceptEdits；结果用 `--json-schema` 约束。顺便实测 `--input-format stream-json` 能否在一个进程里连续发消息。
5. **反方向入口**：让 Codex 和 agy 能调用 CC。要定三件事：
   - 怎么调：通过 AGENTS.md、skill 或 agy 规则，以后再包成 MCP 工具。
   - 上下文怎么打包：读 `~/.codex/sessions` 和 agy 的会话记录。
   - 怎么防止互相无限转包：设调用深度上限，记账时记下是谁发起的。
6. **两台机器**：在 Mac mini 上安装，跑下文的第一个实验。
7. （可选）把我们的工具对外包装成 ACP agent，只在想让第三方工具调用时才做。

顺序：1、2 先做，之后按 3 → 4 → 5 推进，6 可以穿插进行。各步骤都在 `baseline/cc-delegate` 开发，经用户同意后再装回 `~/.claude/tools/cc-delegate`。

## 暂不做

- **`cc_delegate.py` 暂不拆分**（2026-11-23 定），继续保持单文件。理由有四条：
  - 拆分只改结构、不改行为，是重构，应和修 bug 分开做；
  - 改动会覆盖全部 2400 多行，风险大于收益；
  - 单文件是有意的设计：要打包给非技术用户，隐藏子命令依赖 `Path(__file__)`，测试和装回流程也都按单文件处理；
  - 驱动接口已经把各个 agent 的协议分开了，目前没有实际痛点。

  等以后加功能时真的反复被单文件拖累再拆。到时可以开发时分成多个模块，安装时再合成一个文件。

## 已完成
- 第 5 项的 Codex 发起方已在仓库实现。通过 `run --caller codex` 调用 Claude 或 agy，按当前线程或指定 rollout 打包对话，任务回执和记账记录发起方。两家只读和写入任务的实测见 [Codex 委托实验](../experiments/2026-11-24-codex-caller.md)。agy 发起方和上层入口仍待定。
- 放开写任务的并行限制（每个 `--onto` 分支同时只允许一个任务），以及 Codex 守护进程后端 `--backend daemon`。两项都已装回。
- 第 1 项：`steer`、`cancel`、`status` 三个短命令；第 3 步实测记录见 [experiments/2026-11-23-codex-import](experiments/2026-11-23-codex-import/README.md)。
- 第 2 项：拆出驱动接口，现有三个驱动是 `codex-exec`、`codex-daemon`、`agy-print`。
- 后两项也已装回。
- 第 3 项：通用 ACP 驱动（`--to agy --backend acp`），写入由我们自己的 Seatbelt 沙箱限制。实测见 [experiments/2026-11-23-agy-acp](experiments/2026-11-23-agy-acp/README.md)。默认仍是 `agy -p`。已装回。
- 第 4 项：Claude 驱动（`--to claude`，`claude -p`）。只读用 `dontAsk` 加限定工具；写入用 `bypassPermissions`，边界由 Seatbelt 保证（`acceptEdits` 下复合命令没人审批，提交做不成）。`~/.claude` 只放开 Claude 自己的状态目录。另加了上层的防递归（`CC_DELEGATE_DEPTH`，默认最多两层）。实测见 [experiments/2026-11-23-claude-print](experiments/2026-11-23-claude-print/README.md)。stream-json 连续发消息还没测。
- 小修：agy 写入任务默认改走 ACP（只读仍是 `agy -p`；没有 Seatbelt 时自动退回，显式指定 `--backend acp` 则拒绝）；理由是 `agy -p` 写入时复合命令被拒、提交做不成，而 ACP 能提交、能续接和取消。新增 `prune` 清理结束超过 7 天的运行目录、克隆和已合并的 `delegate/*` 分支，未合并的分支保留；测试临时目录集中到被忽略的 `.test-tmp/`；Codex 守护进程任务只要有会话就给 `resume_command`。第 4 项和这些小修都已装回。
- 路由规则双审（Astra 加 Neon 编排模型）发现的改派问题已修复并装回：改派会按 `CC_DELEGATE_CODEX_BACKEND` 选后端；带 `--onto` 的写入任务也能改派；带 `--resume` 的任务不再改派；只读任务改派时自动加 `--search`，并沿用模型名里的强度档位；`--context none` 也会附上 `--include` 的文件。
- 已知遗留四条和 ACP 记账问题已修：任务期间又改了原本就有改动的文件，现在能检测到（只比文件大小和修改时间）；其他 `delegate/*` 分支被改动会报越界（并行任务的正常落地不算）；没有新提交时不再取回空分支；只读模式下给 `--base` 或 `--onto` 会直接报错；ACP 的 `total_tokens` 改为输入加输出，和 `agy -p` 一致。已装回。
- 第 6 项：Mac mini 上装好并实测通过（Codex、agy、claude 各一次只读调用，agy 一次写入任务；沙箱照样拦住越界写入）。在那台机器上通过 SSH 使用时，macOS 钥匙串是锁着的：claude 改用 `CLAUDE_CODE_OAUTH_TOKEN` 长期令牌，agy 在 SSH 会话里登录，凭据会存进它自己的文件存储。
- `--effort` 增加 `max` 和 `ultra`：本机 Codex 的模型缓存显示 `gpt-6.2-astra` 支持这两档，`ultra` 会自动拆子代理；Codex 不写 `--effort` 时默认是 `low`。其他接单方不支持的档位由它们自己的 CLI 拒绝。
