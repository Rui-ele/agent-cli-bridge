# Agent CLI Bridge

在 Claude Code 会话里，可以把一次代码审阅交给 Codex，也可以让 Antigravity CLI（agy）在独立克隆中修改代码，再把结果带回原会话。本仓库提供负责传递任务、上下文和结果的 Python 脚本 [cc_delegate.py](baseline/cc-delegate/cc_delegate.py)，并记录这些 CLI 在 macOS 上的实机表现。

目前已在 MacBook Pro 上验证从 Claude Code 发起委托，由 Codex、agy 或 Claude Code 接单。从 Codex 或 agy 会话发起委托，以及在 Mac mini 上运行完整流程，仍待验证。

## 试一次只读委托

下面的命令从仓库根目录运行。需要 Python 3，以及已登录、可调用所选模型的 agy。用 `agy models` 查看当前账号可用的模型 ID。

```sh
python3 baseline/cc-delegate/cc_delegate.py run \
  --to agy --model gemini-pro-orchestrator-neon --no-fallback \
  --mode ro --context none --include AGENTS.md \
  --task '根据附带的 AGENTS.md，说明写任务应该在哪个分支上进行。'
```

命令只附上 `AGENTS.md`。成功时，终端回执中会出现 `status="ok"`，`summary` 应说明每项写任务在功能分支上进行，并用 `--onto` 指定该分支。

需要修改代码时，使用 `--mode write --onto <已有功能分支>`。接单方在独立克隆中提交改动，脚本再尝试将指定分支快进到该提交。

[开发约定](AGENTS.md) · [实机实验](experiments/) · [调研笔记](research/) · [MIT 许可证](LICENSE)
