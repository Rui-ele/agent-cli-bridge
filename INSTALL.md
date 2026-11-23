# 安装指南（写给执行安装的 agent）

这份指南写给 Claude Code（CC）看：用户把本仓库的链接交给你，请你在**这台 Mac** 上把 cc-delegate 从零装好。照下面的步骤顺序做，每一步先验证，验证通过再往下走。用户的说明书是 [README.md](README.md)，这里只写安装。

## 先读：规则

- **命令**：下面的命令都能在 macOS 的 zsh 里直接运行。每次 Bash 调用都是一个新 shell，变量不会保留：带 `PY=…` 的命令，每次都要把第 3 步记下的路径写在命令开头。
- **只有用户本人能做的事**：在弹出的系统窗口里点「安装」、输入 Mac 登录密码、安装各 CLI、在浏览器里登录账号。遇到这些，停下来，用一两句话告诉用户要做什么、做完回复你，**等用户确认后再继续**。不要替用户输入密码，也不要用 `sudo`。
- **改动用户的配置前先问**：往 `~/.zshrc` 加内容、覆盖已有的安装或技能文件，都先征得用户同意。
- **失败了怎么办**：每一步都写了。没写到的情况，把命令和完整输出给用户看，不要自己猜着绕过去。
- 仓库里的 `AGENTS.md` 写的是在本仓库**开发**的规矩（例如「不要自己覆盖安装目录」）。现在是用户本人请你安装，不受那一条限制，但覆盖已有安装前仍要先问用户。

装完后的样子：

| 位置 | 内容 |
|---|---|
| `~/.claude/tools/agent-cli-bridge/` | 仓库的克隆，以后用它升级 |
| `~/.claude/tools/cc-delegate/` | 实际运行的脚本（从克隆里复制） |
| `~/.claude/skills/delegate/SKILL.md` | 让 CC 学会委托的技能 |

## 第 1 步：确认是 macOS

```zsh
uname -s && sw_vers -productVersion
```

- **通过**：第一行是 `Darwin`。
- **不通过**：告诉用户这个工具只支持 macOS（Linux 只能部分使用，见 README「前提」），然后停止。

## 第 2 步：git（Xcode 命令行工具）

```zsh
xcode-select -p && git --version
```

- **通过**：打印一个路径和 `git version …`。
- **不通过**（报错，或弹出「需要安装命令行开发者工具」的窗口）：运行下面这条，它会弹出系统窗口：

  ```zsh
  xcode-select --install
  ```

  然后告诉用户：「屏幕上弹出了安装命令行开发者工具的窗口，请点『安装』并同意许可，装完（可能要几分钟到半小时）后告诉我。」等用户确认后，重新运行本步的检查命令。

## 第 3 步：Python 3.10 或更高

macOS 自带的 `/usr/bin/python3` 是 3.9，不能用。先找本机有没有合格的 Python：

```zsh
for p in python3 python3.14 python3.13 python3.12 python3.11 python3.10 "$HOME/.local/bin/python3.12" /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  exe=$(command -v "$p" 2>/dev/null) || continue
  "$exe" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && { echo "PY=$exe"; "$exe" --version; break; }
done
```

- **通过**：输出 `PY=/某个/绝对路径` 和版本号。**记下这个绝对路径**，下文的 `PY=…` 都填它。
- **没有输出**：本机没有合格的 Python，按下面三种办法装一个。装完重新运行上面的查找命令。
  1. **有 Homebrew**（`command -v brew` 有输出）：运行 `brew install python`。
  2. **没有 Homebrew，用 uv**（推荐，不需要密码，全程你可以自己做）。先告诉用户你要从 [uv 官网](https://docs.astral.sh/uv/) 安装 uv，再用它装 Python 3.12，都装在用户目录下，征得同意后运行：

     ```zsh
     curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh && "$HOME/.local/bin/uv" python install 3.12
     ```

     装好后 Python 在 `~/.local/bin/python3.12`。
  3. **用户更想用图形安装包**：请用户打开 <https://www.python.org/downloads/macos/>，下载最新的 macOS 安装包，双击安装（需要输入 Mac 密码），装完告诉你。

  三种办法都失败时，把输出给用户看，停止。

## 第 4 步：下载仓库

```zsh
mkdir -p "$HOME/.claude/tools" && cd "$HOME/.claude/tools" && if [ -d agent-cli-bridge/.git ]; then git -C agent-cli-bridge pull --ff-only; else git clone https://github.com/Rui-ele/agent-cli-bridge.git; fi && git -C agent-cli-bridge log --oneline -1
```

- **通过**：最后一行是一个提交号和提交说明。
- **不通过**：
  - 网络错误：请用户检查网络，或者确认能在浏览器里打开 <https://github.com/Rui-ele/agent-cli-bridge>。
  - `pull --ff-only` 失败：这个目录里有本地改动。把 `git -C "$HOME/.claude/tools/agent-cli-bridge" status` 的输出给用户看，问用户怎么处理，不要自行丢弃改动。
  - `agent-cli-bridge` 已存在但不是 git 仓库：问用户能否把它改名备份后重新克隆。

## 第 5 步：复制到安装目录

先看是否已经装过：

```zsh
ls -d "$HOME/.claude/tools/cc-delegate" 2>/dev/null || echo 未安装
```

- 输出 `未安装`：直接运行下面的复制命令。
- 已存在：告诉用户「已经有一份旧的安装，我会把它改名为 `cc-delegate.bak-日期` 备份，再装新的」，用户同意后再运行复制命令。

```zsh
dst="$HOME/.claude/tools/cc-delegate"; if [ -e "$dst" ]; then mv "$dst" "$dst.bak-$(date +%Y%m%d%H%M%S)"; fi; cp -R "$HOME/.claude/tools/agent-cli-bridge/baseline/cc-delegate" "$dst" && PY=填第3步的路径 && "$PY" "$dst/cc_delegate.py" --help | head -3
```

- **通过**：输出以 `usage: cc_delegate.py` 开头。
- **输出「需要 Python 3.10 或更高」**：`PY` 填错了，回到第 3 步。

## 第 6 步：离线测试

测试不联网、不调用真实的 CLI，大约一两分钟。用后台方式运行，等它结束：

```zsh
PY=填第3步的路径 && cd "$HOME/.claude/tools/cc-delegate" && "$PY" -m unittest discover -s tests 2>&1 | tail -5
```

- **通过**：最后是 `Ran … tests` 和 `OK`。
- **不通过**：
  - 只有一两个和时间有关的测试失败（名字里有 `stall`、`activity`、`timeout`，或报错里有 `stalled`）：机器忙时可能偶发，再跑一次。第二次通过就算通过，并告诉用户第一次的失败。
  - 其他失败，或者重跑仍失败：把失败的测试名和报错给用户看，建议到 <https://github.com/Rui-ele/agent-cli-bridge/issues> 反馈，然后停止。

## 第 7 步：接单方 CLI（只能由用户安装和登录）

cc-delegate 把任务交给本机的 Codex（`codex`）、Antigravity CLI（`agy`）或另一个 Claude Code（`claude`）。至少要有一个装好并登录。

先检查装了哪些：

```zsh
for c in codex agy claude; do printf '%s: ' "$c"; command -v "$c" || { [ -x "$HOME/.local/bin/$c" ] && echo "$HOME/.local/bin/$c（不在 PATH 上）"; } || echo 未安装; done
```

**有显示「不在 PATH 上」的**：CLI 装在 `~/.local/bin`，但 shell 找不到它，委托时会报 `executable not found in PATH`。征得用户同意后，把这个目录加进 `~/.zshrc`：

```zsh
grep -qs 'HOME/.local/bin' "$HOME/.zshrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
```

这一行只对新开的 shell 和新的 CC 会话生效。**本次会话**里后面的命令，都在开头加上 `export PATH="$HOME/.local/bin:$PATH" &&`。

**一个都没装**：问用户有哪家的订阅，把对应的官方安装说明发给用户，请用户自己安装并登录，装好后告诉你：

| CLI | 官方安装说明 | 说明 |
|---|---|---|
| Codex | <https://developers.openai.com/codex/cli> | 需要 ChatGPT 账号或 OpenAI API key |
| agy | <https://antigravity.google/docs/cli/install> | 需要 Google 账号 |
| Claude Code | <https://code.claude.com/docs/en/setup> | Claude 桌面版自带的 `claude` 不在 PATH 上，终端里找不到，要另外装一次命令行版。委托给它花的是 Claude 的额度 |

用户确认装好后，重新运行上面的检查命令。

**检查登录**（只查已装的那几个）：

```zsh
codex login status
```

```zsh
claude auth status --text
```

```zsh
agy models | head -5
```

- **通过**：codex 显示 `Logged in …`；claude 显示已登录的账号；agy 列出模型。
- **未登录**：请用户在「终端」App 里自己运行 `codex login`、`claude auth login` 或 `agy`，按提示在浏览器里登录，完成后告诉你，然后重新检查。不要替用户完成登录。`agy models` 如果一直不返回或要求登录，也按未登录处理。

**记下要用的模型名**，委托时 `--model` 必填：

- codex：运行 `grep '^model' "$HOME/.codex/config.toml"`，有输出就用里面的模型；没有就问用户平时在 Codex 里用哪个模型。
- agy：从 `agy models` 的列表里选一个，问用户偏好。
- claude：可以用 `sonnet` 或 `opus` 这类别名。

## 第 8 步：安装技能

技能让 CC 知道什么时候、怎么委托。先看是否已有：

```zsh
ls "$HOME/.claude/skills/delegate/SKILL.md" 2>/dev/null || echo 没有
```

已存在时，问用户能否覆盖（会先备份为 `SKILL.md.bak`）。然后：

```zsh
d="$HOME/.claude/skills/delegate"; mkdir -p "$d" && { [ ! -e "$d/SKILL.md" ] || cp "$d/SKILL.md" "$d/SKILL.md.bak"; } && cp "$HOME/.claude/tools/cc-delegate/skill/SKILL.md" "$d/SKILL.md" && head -5 "$d/SKILL.md"
```

- **通过**：输出以 `---` 和 `name: delegate` 开头。

接着改两处（用你的文件编辑工具改 `~/.claude/skills/delegate/SKILL.md`）：

1. **Python 路径**：如果第 3 步的 `PY` 不是 `python3` 能找到的那个（即 `command -v python3` 的输出和 `PY` 不同），把文件里所有 `python3 ~/.claude/tools/cc-delegate/cc_delegate.py` 中的 `python3` 换成 `PY` 的绝对路径。
2. **「我的分工」**：这一节现在是注释里的例子。问用户两三个问题：装了哪几个 CLI、各用什么模型、哪类活想交给谁（例如代码和审阅给 codex，文档和头脑风暴给 agy）。按回答写成几条短规则，替换掉注释。用户说不清时，就只写第 7 步里已登录的 CLI 和模型，并注明「以后可以再改」。

技能在**新开的 CC 会话**里生效。

## 第 9 步：真实的只读委托

用刚克隆的仓库做一次只读委托，确认整条链路能走通。这一步会用掉接单方的少量额度，先告诉用户。从第 7 步里已登录的 CLI 中选一个，填上 `--to` 和 `--model`：

```zsh
PY=填第3步的路径 && "$PY" "$HOME/.claude/tools/cc-delegate/cc_delegate.py" run --to codex --model 填模型名 --mode ro --project-dir "$HOME/.claude/tools/agent-cli-bridge" --task '读 README，用三句话说明这个项目是做什么的。'
```

用后台方式运行，等它结束，通常一到几分钟。

- **通过**：最后十几行是回执，里面有 `status="ok"` 和几条 `summary=`。
- **不通过**，看回执里的 `status` 和 `summary`：
  - `executable not found in PATH`：回到第 7 步，处理 PATH。
  - `summary` 只写了 `worker_failed`：接单方自己的报错在回执 `run_dir` 目录下的 `last.txt`，用 `cat` 看一眼。例如 Claude 未登录时，`last.txt` 里是 `Not logged in`。
  - 提到登录、认证、401、403、`Not logged in`：请用户重新登录对应的 CLI（见第 7 步）。
  - 提到模型不存在或无权使用：换一个模型名再试。
  - 其他错误：把回执给用户看。完整报告在回执的 `report_path`，事件流在 `run_dir` 里。

## 第 10 步：告诉用户结果

用几句话告诉用户：

- 装在哪里，用的哪个 Python，哪几个 CLI 已登录，测试和试跑的结果；
- 以后在**新的 CC 会话**里说「把这个交给 codex 审一下」之类的话，CC 就会用技能去委托；
- 使用前请读 README 的「默认行为和边界」一节：接单方能读到这个账号能读的所有文件；
- 以后升级：再让 CC 按这份指南走一遍，第 4 步会拉最新代码，第 5 步会备份旧版再装新版。
