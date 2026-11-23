# Agent CLI Bridge

在 Claude Code 会话里，可以把一次代码审阅交给 Codex，也可以让 Antigravity CLI（agy）在独立克隆中修改代码，再把结果带回原会话。本仓库提供负责传递任务、上下文和结果的 Python 脚本 [cc_delegate.py](baseline/cc-delegate/cc_delegate.py)，并记录这些 CLI 在 macOS 上的实机表现。

## 试一次只读委托

下面的命令从仓库根目录运行。需要 Python 3，以及已登录、可调用所选模型的 agy。用 `agy models` 查看当前账号可用的模型 ID。

```sh
python3 baseline/cc-delegate/cc_delegate.py run \
  --to agy --model gemini-pro-orchestrator-neon --no-fallback \
  --mode ro --context none --include AGENTS.md \
  --task '根据附带的 AGENTS.md，说明写任务应该在哪个分支上进行。'
```

这次委托只附上指定文件，跳过发起方的会话历史。`--no-fallback` 让 agy 失败时直接报告结果，方便确认任务由谁执行。

## 写任务如何交回改动

写任务使用 `--mode write`。如果希望结果落到已有功能分支，通过 `--onto <分支名>` 指定目标。接单方在独立克隆中工作并提交，脚本再尝试把目标分支快进到该提交。不同目标分支的任务可以并行，同一目标分支一次只接受一个写任务。

## 已验证的接单端

| 接单工具 | 可用后端 | 实机记录 |
| --- | --- | --- |
| Codex | `codex exec` 或 app-server 守护进程。守护进程后端支持查看状态、中途补充指令和取消任务。 | [守护进程与并行写入](experiments/2026-11-23-multithread/README.md) |
| Antigravity CLI | 默认只读走 `agy -p`。写入时如有 macOS Seatbelt 沙箱，默认走 `agy acp`。 | [ACP 权限与写入](experiments/2026-11-23-agy-acp/README.md) · [模型调用实测](experiments/2026-11-23-agy-model-smoke.md) |
| Claude Code | 通过 `claude -p` 接单。只读任务限制可用工具，写任务由 Seatbelt 约束写入范围。 | [Claude 非交互调用](experiments/2026-11-23-claude-print/README.md) |

目前的发起入口是 MacBook Pro 上的 Claude Code。Claude 已能作为接单端运行；让 Codex 或 agy 从自己的会话发起委托并取回结果，仍是后续工作。Mac mini 上的端到端流程也待验证。

## 开发与测试

脚本只依赖 Python 标准库。以下测试使用合成会话、模拟 CLI 和假守护进程，可以离线运行。

```sh
cd baseline/cc-delegate
python3 -m unittest discover -s tests
```

[开发约定](AGENTS.md) · [实机实验](experiments/) · [调研笔记](research/) · [MIT 许可证](LICENSE)
