# 路由规则 v2 的双审（2026-11-23）

发起方：用户在路由规则讨论会话里提出，交给主对话（CC）执行。审阅对象是刚定下的第二版分工规则（`~/.claude/model-routing.md`）、同步改过的委托技能说明（`~/.claude/skills/delegate/SKILL.md`），以及把 Codex 默认后端设为 daemon 的环境变量。规则文件不在本仓库里；这里只记审阅过程和结论。

## 做法

- 两份审阅并行，都是只读、`--context none`，只给规则文件、改前的备份和 cc-delegate 的 README，不带 CC 的倾向。
  - Astra：codex `gpt-6.2-astra`，high，daemon 后端，69 秒。
  - Neon 编排模型：agy `gemini-pro-orchestrator-neon`，`agy -p`，607 秒，思考 token 约 54k。
- 要求分两级：说得出触发场景的严重问题，和一般建议；只列有把握的。
- CC 逐条对照 `cc_delegate.py` 和规则文件核实，再合并。

**一次作废的运行（CC 的失误）**：zsh 不按空格拆分未加引号的变量，5 个 `--include` 路径被当成一个参数传了进去。agy 因此收到一个畸形的 `--add-dir`，被拒读主文件，交回空回复。改成逐个传路径后重跑。Astra 那份没受影响，因为任务文本里写了路径，它自己去读了。

## 结论（均已核实）

两份一致的严重问题：
1. agy 写入任务带 `--onto` 时，失败后的自动改派必然失败：`run()` 原地把 `--onto` 换算成了 `args.base`，改派复制参数后撞上 `onto_and_base_conflict`。
2. `--context none` 完全不打包 `--include`，worker 连文件名都看不到。「大方案」规则要求用 `--context none` 加 `--include`，所以它是落空的。
3. 影子跑和大方案的第二意见遇到 agy 失败，会被静默改派给 Astra，回执里只剩 Astra 的结果，记账会记错模型。
4. 读取防护有缺口：影子跑可能把写任务派给 agy；只读任务显式走 ACP 时，也没有 `agy -p` 那组读拒绝规则。

只有一方提出的：
- 改派会丢失 `--resume` 的上下文（Astra）。也会丢失联网能力，因为 agy 任务不加 `--search`；以及写在模型名后缀里的强度档位（Neon）。
- 「读代码 → Astra」和「给用户看的报告 → Neon」在「读代码后写报告」这类任务上冲突。Neon 列为严重，Astra 列为建议。
- 回执给出的 `rollback` 命令作用于执行时的当前分支，验收期间切过分支就会退错（Astra）。
- 文档不一致：`--to claude` 的启用条件，以及过时的卡死判定说明（Astra）。
- Astra 单审 Astra 写的代码，有同源盲区（Neon）。

## 处理

- 代码：修复第 1、2 条，以及改派时丢失的 `--resume`、`--search` 和强度档位，见提交 `1218aa6`。修改前，路由讨论会话已经让改派也遵循 `CC_DELEGATE_CODEX_BACKEND`（`029e9b6`）。新测试在旧代码上会失败，在新代码上通过。
- 规则：影子跑和第二意见一律加 `--no-fallback`；Neon 的影子只跑只读任务；含密钥或凭据的目录不派给 agy；结论来自读代码或调研的报告由 Astra 出结论、CC 改写；审阅保持单审，但改用 `--context none`；回退前先核对分支；统一 `--to claude` 的启用条件。
- 不采纳：恢复双审。单审是用户刻意定的。

## 未核实

- 两份审阅里，Neon 那份是第二次运行的结果；第一次因为 CC 传参出错作废了。没有再让 Astra 用同样的方式重跑一遍做对照。
