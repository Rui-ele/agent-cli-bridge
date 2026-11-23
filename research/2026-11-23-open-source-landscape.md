# 开源项目调研：本地 AI agent 互相调用（2026-11-23）

调研分工：Neon（agy gemini-4-neon-high）和 Astra（codex gpt-6.2-astra，开启联网搜索）各查一遍，CC 合并、核对后改写。仓库是否存在、star 数和最近一次推送时间，都由 CC 用 GitHub API 逐个核实过。下文的关键机制，CC 已从 README 或源码中核实，见各条标注；其余是两份报告的说法，未经 CC 核实。

## 结论

1. 主流做法有五类：用 MCP 包装 CLI、ACP 协议、厂商官方 SDK 或 app-server、tmux 终端驱动、看板或工单式编排。
2. **OpenAI 官方的 [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)**（35.4k★）就是「从 Claude Code 调 Codex」，底层走 app-server，外加一个 broker 进程（源码里有 `app-server-broker.mjs`，CC 已核实）。这说明我们打算研究的 app-server 路线，官方自己就在用。
3. **双向调用已有现成实现**：[raysonmeng/agent-bridge](https://github.com/raysonmeng/agent-bridge)（436★，活跃）用一个常驻守护进程，把 Claude Code 的 MCP channel 和 Codex 的 app-server 接起来，两边都能在对方运行途中推送消息。往 Codex 发消息用的是 `turn/start`（CC 已从 README 核实）。反方向「在 Codex 里用 Claude Code」有 [sendbird/cc-plugin-codex](https://github.com/sendbird/cc-plugin-codex)（234★）。
4. **ACP 路线覆盖最广**：[openclaw/acpx](https://github.com/openclaw/acpx)（3.5k★）一个命令就能驱动 Codex、Claude Code、Gemini CLI、Antigravity，支持具名会话续接（CC 已从 README 核实）。两份报告都推荐它作为补上反向调用的最小试点。
5. **常见的坑是权限**：[PAL clink](https://github.com/BeehiveInnovations/pal-mcp-server) 预设用 `codex exec --dangerously-bypass-approvals-and-sandbox` 和 `gemini --yolo` 启动（CC 已从文档核实）。tmux 类工具 [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) 给 Codex 默认的参数也是 `--yolo`（未核实）。我们脚本里的 worktree 隔离和只读档位，正是这些项目普遍缺少的。

## 分类表

| 类别 | 代表项目（★，最近推送） | 怎么调用 | 和我们的关系 |
| --- | --- | --- | --- |
| MCP 包装 CLI | [PAL](https://github.com/BeehiveInnovations/pal-mcp-server)（11.8k，2025-12，已停更）；加了 agy 的维护分支 [serpro69/pal-mcp-server](https://github.com/serpro69/pal-mcp-server)（7，活跃）；[jamubc/gemini-mcp-tool](https://github.com/jamubc/gemini-mcp-tool)（2.3k）；[mkXultra/ai-cli-mcp](https://github.com/mkXultra/ai-cli-mcp)（27）；[steipete/claude-code-mcp](https://github.com/steipete/claude-code-mcp)（1.3k，**已归档**） | 每次调用都新起一个 CLI 进程，结果作为 MCP 工具的返回值 | 和我们重复最多。可借鉴的是让各端共用同一个 MCP 工具当入口 |
| ACP | [acpx](https://github.com/openclaw/acpx)（3.5k）；[agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol)（4.7k）；[codex-acp](https://github.com/agentclientprotocol/codex-acp)（591）；[claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp)（2.8k） | 统一协议，具名会话；同一会话内排队，不同会话并行 | 最值得试，可以用来补反向调用 |
| 官方 SDK / app-server | [codex-plugin-cc](https://github.com/openai/codex-plugin-cc)（35.4k）；[cc-plugin-codex](https://github.com/sendbird/cc-plugin-codex)（234）；[claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)（8.6k）；[openai/symphony](https://github.com/openai/symphony)（28.1k） | 常驻进程加协议，可以在后台运行、查看状态、取消 | codex-plugin-cc 的 broker 值得读源码 |
| 双向桥 | [agent-bridge](https://github.com/raysonmeng/agent-bridge)（436） | 守护进程在 MCP channel 和 app-server 之间转发消息，消息按标签过滤 | 和本仓库「双向」的目标最接近 |
| tmux / 看板编排 | [cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator)（1.6k）；[claude-squad](https://github.com/smtg-ai/claude-squad)（8.9k）；[vibe-kanban](https://github.com/BloopAI/vibe-kanban)（28.8k）；[agent-orchestrator](https://github.com/OrchestratorInc/agent-orchestrator)（15k）；[gastown](https://github.com/gastownhall/gastown)（18.9k）；[agent-deck](https://github.com/asheshgoplani/agent-deck)（1.3k）；[workmux](https://github.com/raine/workmux)（3.1k）；[dmux](https://github.com/standardagents/dmux)（1.8k）；[oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode)（41k） | 由人在终端或看板里盯着、管理多个 agent | 定位是「人管多个 agent」，和「agent 调 agent」方向不同 |

## 可以接着做的

- 读 codex-plugin-cc 的 `app-server-broker.mjs`，看官方怎么处理会话、并发和审批。
- 读 agent-bridge，看双向消息怎么防止来回循环、怎么控制传过去的上下文量。
- 用 acpx 做一次反向调用试点：Codex 调 `acpx claude`，回执沿用现有的 schema。
