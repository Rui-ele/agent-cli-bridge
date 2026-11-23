# agy 三个新模型的冒烟测试（2026-11-23，MacBook Pro）

发起方：路由规则讨论会话请主对话（CC）执行。经已装的 cc-delegate 调用 agy 1.5.14。要测的三个模型都出现在 `agy models` 的列表里，但之前没在 cc-delegate 里跑过：`gemini-pro-orchestrator-neon`、`gemini-4-argon-high`、`gemini-4-neon-xhigh`。

做法：只读、`--context none`，任务是读仓库根目录的 `AGENTS.md`，列出一级标题和全部二级标题（标准答案是 6 个二级标题）。每个模型在 `agy -p`（默认）和 `--backend acp` 下各跑一次，每种后端 3 个模型并行。

## 结果（已核实）

| 模型 | 后端 | 结果 | 耗时 | token（输入 / 输出 / 思考） |
| --- | --- | --- | --- | --- |
| gemini-pro-orchestrator-neon | agy -p | ok，答对 | 38.8 秒 | 12.7k / 2.5k / 2.3k |
| gemini-pro-orchestrator-neon | acp | ok，答对 | 33.0 秒 | 14.1k / 2.5k / 2.3k |
| gemini-4-argon-high | agy -p | ok，答对 | 25.6 秒 | 12.8k / 0.6k / 0.3k |
| gemini-4-argon-high | acp | ok，答对 | 30.8 秒 | 13.2k / 1.3k / 1.1k |
| gemini-4-neon-xhigh | agy -p | ok，答对 | 28.6 秒 | 13.1k / 1.2k / 1.0k |
| gemini-4-neon-xhigh | acp | ok，答对 | 34.6 秒 | 13.4k / 3.1k / 2.7k |

- 模型名：在 ACP 下，orchestrator 的名字不带后缀，本身就是会话 `model` 选项的一个可选值。另外两个按后缀拆成 `model` 加 `thought_level`，也都被接受。
- 6 次都没有卡死、改派或打转告警。

## 同日的补充观察

当天稍晚，用 orchestrator-neon 做了一次较重的只读审阅（见 [2026-11-23-routing-v2-review.md](2026-11-23-routing-v2-review.md)）：耗时约 10 分钟，思考 token 约 54k，600 秒的静默兜底没有误判。不过它只调用了 `read_file`，没有派子代理。

## 未核实

- orchestrator 派 Hydrogen 当子代理时，会不会被静默兜底误判成卡死，或者误触发改派 Codex。两次运行都没有派子代理。
- 记账口径：两个后端回执里的 `total_tokens` 算法不同。`agy -p` 是输入加输出；ACP 还加上了缓存读，可能把缓存重复计了一次。比较时应看输入和输出两项。代码没改。
- 每种组合只有一次样本、一个小任务，耗时只能说明大致的量级。
