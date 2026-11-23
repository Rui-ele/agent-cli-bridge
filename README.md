# Agent CLI Bridge

在一个 Claude Code 会话里，你可以把同一项代码审阅或修改任务交给 **Codex、Claude Code 或 Antigravity CLI（agy）**。脚本把任务和所选上下文传给接单方，再把结果带回发起会话。

目前已在 MacBook Pro 上验证由 Claude Code 发起委托。从 Codex 或 agy 会话发起委托，以及在 Mac mini 上运行完整流程，仍待验证。

## 试一次只读委托

在仓库根目录运行下面的命令。需要 Python 3，并已登录所选接单方的 CLI。`WORKER` 可选 `codex`、`claude` 或 `agy`，`MODEL` 填该 CLI 中你的账号可用的模型 ID。下面以 Codex 为例，切换接单方只需改前两行。

```sh
WORKER=codex
MODEL=gpt-6.2-astra

python3 baseline/cc-delegate/cc_delegate.py run \
  --to "$WORKER" --model "$MODEL" \
  --mode ro --context none --include AGENTS.md \
  --task '根据附带的 AGENTS.md，说明写任务应该在哪个分支上进行。'
```

命令只附上 `AGENTS.md`。成功时，终端回执中会出现 `status="ok"`，`summary` 应说明每项写任务在功能分支上进行，并用 `--onto` 指定该分支。

需要修改代码时，使用 `--mode write --onto <已有功能分支>`。接单方在独立克隆中提交改动，脚本再尝试将指定分支快进到该提交。

[开发约定](AGENTS.md) · [实机实验](experiments/) · [调研笔记](research/) · [MIT 许可证](LICENSE)
