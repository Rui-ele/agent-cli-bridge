# 同一个 Codex app-server 能否同时跑多个 thread、并行写入（2026-11-23，MacBook Pro）

codex-cli 0.171.0，模型 gpt-6.2-astra。每个 thread 都设为 `sandbox: "read-only"`、`approvalPolicy: "never"`、`ephemeral: true`，任务是「执行一次 `sleep 20`，然后回复 done-i」。所有 `turn/start` 同时发出，不等前一个结束。

| 入口 | 脚本 | thread 数 | 实际耗时 | 各轮耗时相加 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 单独启动一个 `codex app-server`（stdio，JSONL） | `stdio_probe.py` | 3 | 28.8s | 83.1s | 并行 |
| 受管守护进程（`~/.codex/app-server-control/app-server-control.sock`，WebSocket） | `daemon_probe.py` | 3 | 29.7s | 82.9s | 并行 |
| 同上 | `daemon_probe.py` | 6 | 28.3s | 159.1s | 并行，耗时基本不随数量增加 |

- 每一轮都以 `completed` 结束。服务端没有发来审批请求，说明只读沙箱足以执行 `sleep`。
- 运行期间调用 `thread/loaded/list`，守护进程报告这次开的 thread 全部处于已加载状态。

## 并行写入（`daemon_write_probe.py`）

在守护进程上同时开 3 个 thread。每个 thread 都在自己的克隆里、自己的分支 `task-i` 上，往**同一个**文件 `shared.txt` 追加一行，新建 `task-i.txt`，`sleep 10`，然后提交。thread 0 额外尝试一次往主 checkout 写入 `escape.txt`。沙箱参数在 `turn/start` 里传：`sandboxPolicy: {type: "workspaceWrite", writableRoots: [<这个 thread 的 git 目录>], networkAccess: false}`。

| 尝试 | 隔离方式 | 结果 |
| --- | --- | --- |
| 1 | `git worktree`，writableRoots 设为主仓库的 `.git` | 并行写文件成功，**提交全部失败**：`git add` 无法创建 `index.lock` |
| 2 | 每个 thread 一个本地克隆，git 目录命名为 `.git-i`（`--separate-git-dir`），writableRoots 设为它 | 3 个都提交成功；实际耗时 22.3s，各轮相加 57.3s，并行 |

尝试 2 的核对（由脚本用 git 检查，不采信 Codex 的自述）：
- 每个分支都比 main 多 1 个提交，只改了 `shared.txt` 和自己的 `task-i.txt`。各分支的 `shared.txt` 里都只有自己那一行，没有混入别的 thread 的修改。
- 主 checkout 干净，main 仍停在 base 提交。
- `escape.txt` 没有创建出来，Codex 报 `Operation not permitted`，说明沙箱挡住了越界写入。
- 服务端没有发来审批请求。

**要点：** Codex 的沙箱会把名为 `.git` 的目录一律设为只读，列进 writableRoots 也没用。所以 `git worktree`（共用主仓库 `.git`）无法在沙箱内提交，必须像 cc_delegate 那样给每个任务一个克隆，并把 git 目录改用别的名字。

三个分支都改了 `shared.txt`，合并时会冲突。这是合并阶段的事，和执行时的隔离无关。

## 顺带发现

- 守护进程的控制 socket 走的是 **WebSocket**：先做 HTTP Upgrade，响应头里有 `x-codex-websocket-max-unfragmented-message-bytes: 16777216`。`codex app-server proxy` 只是原样转发字节，所以经由它的客户端得自己处理 WebSocket；直接往里写 JSONL 收不到任何回复。用 Python 的 `websockets.unix_connect` 直连 socket 即可。
- 单独启动的 `codex app-server` 走 stdio，直接收发 JSONL，官方 codex-plugin-cc 用的就是这种方式。

## 没测的

- 6 个以上能开到多少，以及账号层面的速率限制。
- `ephemeral: false` 的 thread 会不会出现在 `codex agents` 里，供用户查看和接手。

## 运行方式

```sh
python3 stdio_probe.py 3 <工作目录>
python3 daemon_probe.py 3 <工作目录> ~/.codex/app-server-control/app-server-control.sock
python3 daemon_write_probe.py 3 <临时目录，会被清空> ~/.codex/app-server-control/app-server-control.sock
```
