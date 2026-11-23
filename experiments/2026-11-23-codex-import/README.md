# 第 3 步实测：thread 可见性与会话导入（2026-11-23，MacBook Pro）

codex-cli 0.171.0，受管守护进程。脚本见 `probe.py`（用第三方 `websockets` 库，只用于实验）。

## thread 可见性

- cc-delegate 用守护进程后端开的 thread（`ephemeral: false`，按 task_id 命名）出现在守护进程的 `thread/list` 里。`codex agents` 浏览的就是这份列表，所以用户可以在那里找到并接手。没有在 TUI 里逐项点开确认。
- `thread/list` 的 `searchTerm` 搜不到 thread 名，要按 `name` 字段自己筛选。
- 续接时会再次 `thread/name/set`，名字会变成最近一次任务的 task_id。实测：`20261123-113851-…` 开的 thread，续接后名字成了 `20261123-113921-…`。
- 这些 thread 的 `source` 显示为 `vscode`。

## `externalAgentConfig/import`：把 CC 会话导入 Codex

请求（`itemType: "SESSIONS"`）：

```json
{"migrationItems": [{"itemType": "SESSIONS", "description": "…", "cwd": null,
  "details": {"plugins": [], "sessions": [{"path": "<~/.claude/projects/…/<id>.jsonl>", "cwd": "<项目目录>", "title": null}],
              "mcpServers": [], "hooks": [], "subagents": [], "commands": []}}]}
```

之后等通知 `externalAgentConfig/import/completed`，新 thread 的 id 在 `successes[].target` 里。另外 `~/.codex/external_agent_session_imports.json` 也有记录。

| 尝试 | 结果 |
| --- | --- |
| `sessions[].cwd` 传 `null` | 请求被拒：`invalid type: null, expected path string` |
| 把会话文件复制到 `~/.claude/projects` 之外再导入 | 失败：`session_not_detected`。只认 Claude Code 自己目录下的会话文件 |
| 导入当前这个正在进行的会话（3.8 MB） | 0.7 秒完成，生成的 thread 有 27 个 turn，标题沿用 CC 会话名 |

在导入的 thread 上，只读地追问三个只有会话历史里才有的事实（Rui-ele 是谁、为什么委托的 Codex 用不了浏览器、第 1 步落在哪个分支），三个都答对了。这一轮 `inputTokens` 为 126,261，没有命中缓存。

## 结论

- **可以替代 `--context full`**：导入速度快、内容完整，也省掉了我们自己的打包逻辑。
- **不能替代分档打包**：导入带过去的是整个会话，没有裁剪，长会话首轮就要十几万 token。`none`、`scoped`、`session` 三档仍然需要自己打包。
- 适合「把当前对话整个交给 Codex 接着做」这种场景；也适合 cc-delegate 增加一个 `--context import` 档，前提是会话文件在 `~/.claude/projects` 下。
- 导入后的 thread 会出现在用户的 Codex 会话记录里。
