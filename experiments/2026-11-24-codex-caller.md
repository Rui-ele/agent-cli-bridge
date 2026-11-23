# Codex 委托给 Claude Code 和 agy

2026-11-24 在 macOS 上由 Codex 发起，经开发分支的 `cc_delegate.py run --caller codex` 调本机已登录的 CLI。版本为 Codex CLI 0.171.0、Claude Code 2.1.330、agy 1.5.15。以下任务全部使用合成文件、`--context none` 和 `--no-fallback`。

| 接单方 | 后端与模型 | 只读任务 | 写入任务 |
| --- | --- | --- | --- |
| agy | 只读显式指定 `--backend acp`，写入自动选择 ACP；`--model gemini-pro-orchestrator-neon` | 23.4 秒，正确读回测试值 | 47.2 秒，提交取回到指定分支 |
| Claude Code | print；`--model haiku`，用量回执记录实际模型 `claude-haiku-5-5` | 重试 5.3 秒，正确读回测试值 | 7.1 秒，提交取回到指定分支 |

只读任务要求读取临时目录里的 `sample.txt`，两个回执都返回了与文件内容相符的值。首次调 Claude 耗时 3.8 秒，报告中出现 HTTP 401 和 OAuth 已过期。随后用原生 Claude CLI 在同一目录做无工具的应答检查，成功后重新发起委托。没有单独验证是哪一步刷新了凭据。

写入任务在同一临时 Git 仓库从同一基线创建两个目标分支，每家用 `--mode write --onto <目标分支>` 提交一个文件。运行后分别用 `git diff --name-status` 和 `git show` 核对：两个分支各增加了一个指定文件，内容都正确，原工作区干净，主分支仍指向基线。

四个成功任务都用 `status` 读回结果，并用 `mark --outcome accepted` 记录验收。四条验收记录的发起方均为 Codex，CC 成本字段均为空。Codex 对话的自动定位和最近三轮的打包另在本机做了只读检查。外部模型未接收真实聊天记录。续接、取消的本轮验证范围是离线测试。
