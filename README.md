# Agent CLI Bridge

研究本机的 AI 助手如何把任务交给其他模型，并在发起任务的会话里拿回结果。Claude Code 和 Codex 是第一组双向实验。已有的 Antigravity CLI（agy）可以调用 Gemini，未来也可以研究 DeepSeek 等模型通过 CLI 或 API 参与哪些任务。

每次实验分别记录谁发起任务、通过哪个工具接单、实际使用哪个模型。同一模型通过不同工具接入时，可读文件、可用工具和会话续接能力可能不同。目标是在 MacBook Pro 和 Mac mini 上配置用户级入口，让多个业务项目共用。

## 当前进度

| 设备 | Claude Code → Codex | Codex → Claude Code |
| --- | --- | --- |
| MacBook Pro | 已收录现用脚本，待在本仓库复现实机调用 | 已确认 Claude CLI 提供非交互入口，待接入 |
| Mac mini | 待验证 | 待验证 |

[现有脚本](baseline/cc-delegate/cc_delegate.py)和[离线测试](baseline/cc-delegate/tests/test_delegate.py)来自 MacBook 上的用户级安装。脚本还实现了 Claude Code → Antigravity（Gemini）方向。Gemini 发起委派、DeepSeek 的接入方式以及两台机器上的实际表现，后续分别验证。

## 第一个实验

从每一端发起同一个只读任务，让接单方从一份合成材料中找出指定事实，并把答案交回原会话。随后再追问一次，检查能否按会话 ID 续接。用另一台 Mac 重跑，记录 CLI 版本、实际调用的模型、耗时和结果。

验证成功的标准是用户在原会话看到了正确答案，追问能引用上一轮的信息，两台设备从各自的业务项目目录都能发起调用。随后再比较传入完整背景和只传任务及指定文件时的质量、延迟和用量。

## 复现现有离线测试

在仓库根目录运行以下命令。测试使用合成会话和模拟 CLI。

```sh
cd baseline/cc-delegate
python3 -m unittest discover -s tests
```
