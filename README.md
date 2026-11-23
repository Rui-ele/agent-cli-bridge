# Agent CLI Bridge

研究本机的 AI 助手如何把任务交给其他模型，并在发起任务的会话里拿回结果。Claude Code 和 Codex 是第一组双向实验。已有的 Antigravity CLI（agy）可以调用 Gemini，未来也可以研究 DeepSeek 等模型通过 CLI 或 API 参与哪些任务。

每次实验分别记录谁发起任务、通过哪个工具接单、实际使用哪个模型。同一模型通过不同工具接入时，可读文件、可用工具和会话续接能力可能不同。目标是在 MacBook Pro 和 Mac mini 上配置用户级入口，让多个业务项目共用。

## 当前进度

| 设备 | Claude Code → Codex | Codex → Claude Code |
| --- | --- | --- |
| MacBook Pro | 已实机调用：`codex exec` 与 Codex 守护进程两种后端，写任务可并行 | 已确认 Claude CLI 提供非交互入口，待接入 |
| Mac mini | 待验证 | 待验证 |

[脚本](baseline/cc-delegate/cc_delegate.py)和[离线测试](baseline/cc-delegate/tests/test_delegate.py)在本仓库开发，经用户同意后装回 MacBook 的 `~/.claude/tools/cc-delegate`。脚本还实现了 Claude Code → Antigravity（Gemini）方向。Gemini 发起委派、DeepSeek 的接入方式以及两台机器上的实际表现，后续分别验证。实验记录在 [experiments/](experiments/)，调研在 [research/](research/)。

## 路线图

### 分层架构

- **上层**：我们自己的、所有 agent 共用的部分，包括打包上下文、独立克隆隔离、并行锁、快进和回退、结构化回执、记账。
- **底层**：每家 agent 各自的「驱动」。
  - Codex 走 app-server，这是它的原生接口。
  - agy 走 `agy acp`。ACP 是它的原生接口，同时用来验证通用 ACP 驱动。
  - Claude 先用 `claude -p`，以后需要中途插话时，再换 Agent SDK 或 claude-agent-acp。
  - 其他支持 ACP 的 agent 走通用 ACP 驱动。

### 步骤

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

已完成：
- 放开写任务的并行限制（每个 `--onto` 分支同时只允许一个任务），以及 Codex 守护进程后端 `--backend daemon`。两项都已装回。
- 第 1 项：`steer`、`cancel`、`status` 三个短命令；第 3 步实测记录见 [experiments/2026-11-23-codex-import](experiments/2026-11-23-codex-import/README.md)。
- 第 2 项：拆出驱动接口，现有三个驱动是 `codex-exec`、`codex-daemon`、`agy-print`。
- 后两项也已装回。
- 第 3 项：通用 ACP 驱动（`--to agy --backend acp`），写入由我们自己的 Seatbelt 沙箱限制。实测见 [experiments/2026-11-23-agy-acp](experiments/2026-11-23-agy-acp/README.md)。默认仍是 `agy -p`。已装回。
- 第 4 项：Claude 驱动（`--to claude`，`claude -p`）。只读用 `dontAsk` 加限定工具；写入用 `bypassPermissions`，边界由 Seatbelt 保证（`acceptEdits` 下复合命令没人审批，提交做不成）。`~/.claude` 只放开 Claude 自己的状态目录。另加了上层的防递归（`CC_DELEGATE_DEPTH`，默认最多两层）。实测见 [experiments/2026-11-23-claude-print](experiments/2026-11-23-claude-print/README.md)。stream-json 连续发消息还没测。
- 小修：agy 写入任务默认改走 ACP（只读仍是 `agy -p`；没有 Seatbelt 时自动退回，显式指定 `--backend acp` 则拒绝）；理由是 `agy -p` 写入时复合命令被拒、提交做不成，而 ACP 能提交、能续接和取消。新增 `prune` 清理结束超过 7 天的运行目录、克隆和已合并的 `delegate/*` 分支，未合并的分支保留；测试临时目录集中到被忽略的 `.test-tmp/`；Codex 守护进程任务只要有会话就给 `resume_command`。

## 第一个实验

从每一端发起同一个只读任务，让接单方从一份合成材料中找出指定事实，并把答案交回原会话。随后再追问一次，检查能否按会话 ID 续接。用另一台 Mac 重跑，记录 CLI 版本、实际调用的模型、耗时和结果。

验证成功的标准是用户在原会话看到了正确答案，追问能引用上一轮的信息，两台设备从各自的业务项目目录都能发起调用。随后再比较传入完整背景和只传任务及指定文件时的质量、延迟和用量。

## 复现现有离线测试

在仓库根目录运行以下命令。测试使用合成会话和模拟 CLI。

```sh
cd baseline/cc-delegate
python3 -m unittest discover -s tests
```
