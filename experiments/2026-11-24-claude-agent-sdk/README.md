# Claude Agent SDK 运行中追加、中断与续接

2026-11-24，Codex 在 macOS 通过 Anthropic 的 Python 包 `claude-agent-sdk==0.2.187` 和本机 Claude Code CLI 2.1.330 发起实验。指定模型别名 `haiku`，在前三组工具实验的用量回执中核对到 `claude-haiku-5-5`。由 `ClaudeSDKClient` 直接连接 CLI，每组使用独立的临时目录和合成输入。

每条用户消息有独立的 UUID。通过 `ResultMessage.user_message_uuids` 匹配响应实际覆盖的消息，通过答案中的标记核对指令是否生效。下表的时间从该组客户端启动开始计，每组各测一次。

| 追加发生的时机 | 主机动作 | 实际结果 |
| --- | --- | --- |
| 等待写文件审批，默认优先级 | 1.8 秒时收到 `can_use_tool`，文件尚未创建。在审批悬而未决时发送新标记，再允许原写入 | 2.9 秒时的首个结果同时包含原消息与追加消息的 UUID，答案为新标记；文件内容是原指令指定的值。追加在工具完成后的当前轮生效。 |
| 已开始输出文字，默认优先级 | 1.1 秒收到首个 `text_delta`，立即发送新标记 | 2.4 秒收到仅含原消息 UUID 的第一份结果，3.1 秒的第二份结果才包含追加消息 UUID 和新标记。此次默认追加排入下一轮。 |
| 等待写文件审批，指定 `priority="later"` | 2.1 秒时发追加，再拒绝原写入 | 2.7 秒收到原消息的拒绝回执；3.5 秒收到追加消息的单独结果。两次结果同属一个会话，磁盘上没有新文件。 |
| 等待写文件审批，显式中断 | 3.4 秒时调用 `interrupt()`，先读取原轮的结果，再发送追问 | 原轮返回 `error_during_execution` 和 `aborted_tools`。4.1 秒时，同一会话单独返回追问的新标记。被打断的写入未执行。 |

三组涉及 Write 的实验分别观察到 30、31、23 个 `StreamEvent` 帧（顺序为默认追加、显式排队、中断）。在同轮采纳实验之后关闭客户端，再创建带 `resume` 参数的新客户端。新客户端收到“复述上一条消息要求的标记”后，在 6.1 秒时返回正确标记，会话 ID 与关闭前相同。

这里测的是 SDK 的运行语义。实验限制了工具列表和可批准的文件，实际产品中的调用方如何呈现审批、隔离写入及交付提交，由委托脚本负责。[官方流式输入说明](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode) · [Python SDK 参考](https://code.claude.com/docs/en/agent-sdk/python)

## 从 Codex 经过 cc-delegate 的实测

同机用仓库的 `run --caller codex --to claude --model haiku`，自动选中 SDK。上下文档位为 `none`，任务使用合成仓库，四次回执均记录 `claude-haiku-5-5`。

| 场景 | 实际操作与结果 | 耗时（含等待发起方的时间） |
| --- | --- | --- |
| 只读 | 正确读取 `sample.txt` 的合成标签，返回结构化摘要 | 4.5 秒 |
| 写入、审批和追加 | 指定 `--approval ask --mode write --onto <测试分支>`。从 `status` 查到 Git 查询、Write 和提交的工具参数，逐项批准。在第一项审批时通过 `steer` 追加一个标签。同一份 SDK 结果覆盖两条消息，回执带追加标签，分支只增加指定文件 | 43.9 秒 |
| 新克隆续接写任务 | 以同一目标分支和上次的 worker 会话 ID 再发一次写入。新克隆中正确复述前一轮追加的标签，将它作为第二个文件单独提交。检查两个提交的文件内容和主工作区状态均符合预期 | 8.0 秒 |
| 保持客户端、打断与收尾 | 用 `--keep-open` 记住合成标签。首轮结束后 `status` 可读取空闲状态和摘要；用 `queue` 开第二轮，在 Bash 执行期间用 `interrupt --message` 中断。第二轮结果为 `aborted_tools`，第三轮在同会话复述首轮标签。第三轮对附带问题给出部分答案；空闲后再发第四轮，收到正确标签，`finish` 后最终回执为 `ok` | 42.4 秒 |

对四个任务分别复用生成的 Seatbelt 配置，直接尝试在审批目录和原仓库创建测试文件。八次均返回 `Operation not permitted`，对应文件均未创建。

## 复现

先在虚拟环境安装 `claude-agent-sdk==0.2.187`，确保已登录的 `claude` 在 PATH 上。以下命令调用真实服务；探针使用临时目录，不输出会话 ID。

```sh
python probe.py next text later interrupt
```
