# Eval 构建 + Hill-climb：VS Code Copilot 版

这个目录把 Anthropic 官方 `claude-api` skill 里的评测（eval）功能单独提取出来，移植到 **VS Code 的 GitHub Copilot agent 模式**。上游源码在本仓库的 `skills/claude-api/shared/evals/`，提取的是其中的 `build-eval`、`hillclimb`、eval 体检和成本优化四部分。

它只需要 VS Code + Copilot，不依赖 Claude Code、Copilot CLI 或 Codex CLI，适合只允许在 VS Code 里用 Copilot 的公司电脑。Copilot 模型选择器里的主流模型（Claude、GPT、Gemini 等）都能驱动它。

## 能做什么

| 命令 | 作用 |
|---|---|
| `/build-eval <想评估的功能>` | 访谈式地为你的 LLM 应用建一套评测，产出：经你确认的输入集、经你确认的打分方式、可重复运行的脚本、基线报告。 |
| `/hillclimb <要改进什么>` | 在已有 eval 上迭代改进 prompt / skill / 工具描述 / 模型参数 / 代码。流程是：分析 → 修改 → 跑分 → 记录。训练集和测试集分开，最终以测试集上的提升作为结论。 |
| `/eval-audit <eval 在哪>` | 给已有 eval 做体检，检查它的数字是否可信。 |
| `/eval-hillclimb cost ...` | 目标是"质量不降、成本降低"的 hillclimb，按模型 × effort 阶梯搜索。 |

不打斜杠命令，直接说"帮我给这个分类器建个 eval""这个 prompt 能不能再优化"，Copilot 也会自动加载这个 skill。

## 目录结构

```
copilot-eval-hillclimb/
├── .github/
│   ├── skills/
│   │   ├── eval-hillclimb/            # 主 skill（所有逻辑都在这里）
│   │   │   ├── SKILL.md
│   │   │   ├── references/            # 指南：build-eval / eval-hillclimb / eval-audit / cost-hillclimb
│   │   │   │                          #       + vscode-copilot（平台适配）/ data-contract / pricing
│   │   │   └── scripts/               # Node 与 Python 双版本：runner 模板、报告生成器、wait-run、analyzer-slice
│   │   ├── build-eval/SKILL.md        # /build-eval 斜杠命令（薄包装）
│   │   ├── hillclimb/SKILL.md         # /hillclimb 斜杠命令（薄包装）
│   │   └── eval-audit/SKILL.md        # /eval-audit 斜杠命令（薄包装）
│   └── agents/
│       └── eval-analyzer.agent.md     # hillclimb 用的只读分析子 agent
├── tests/run_tests.py                 # 端到端自测
├── README.md / NOTICE.md / LICENSE.txt
```

## 前置条件

- **VS Code 1.110 或更新**（按 1.141 核对过），Copilot Chat 使用 agent 模式。
- **Node 18+ 或 Python 3.8+，有一个就行。** 脚本只用标准库，不用 `npm install` 或 `pip install`。
- **被评测的应用必须能从脚本里调用它的 LLM API**（Anthropic、Azure OpenAI、公司网关等都可以）。如果要用 LLM 当裁判（judge），judge 也要能从脚本调用。**Copilot 自己的模型不能被脚本调用**，所以 judge 通常复用应用自己的 API 通道。

## 安装

两种方式任选：

- **A. 装到项目里**：团队共享，会随仓库提交。
- **B. 装到用户目录**：只对自己生效，并且 skill 文件在工作区之外，agent 改动它时 VS Code 会先询问。

PowerShell（Windows）：

```powershell
$src = "<本仓库路径>\copilot-eval-hillclimb\.github"
# A. 装到项目
Copy-Item -Recurse "$src\skills\*" "<你的项目>\.github\skills\"
Copy-Item "$src\agents\eval-analyzer.agent.md" "<你的项目>\.github\agents\"
# B. 装到用户目录
New-Item -ItemType Directory -Force "$HOME\.copilot\skills", "$HOME\.copilot\agents" | Out-Null
Copy-Item -Recurse "$src\skills\*" "$HOME\.copilot\skills\"
Copy-Item "$src\agents\eval-analyzer.agent.md" "$HOME\.copilot\agents\"
```

bash（macOS / Linux）：

```bash
src=<本仓库路径>/copilot-eval-hillclimb/.github
# A. 装到项目
mkdir -p <你的项目>/.github/skills <你的项目>/.github/agents
cp -R "$src"/skills/* <你的项目>/.github/skills/
cp "$src"/agents/eval-analyzer.agent.md <你的项目>/.github/agents/
# B. 装到用户目录
mkdir -p ~/.copilot/skills ~/.copilot/agents
cp -R "$src"/skills/* ~/.copilot/skills/ && cp "$src"/agents/eval-analyzer.agent.md ~/.copilot/agents/
```

四个 skill 文件夹必须放在同一个 `skills/` 目录下，因为三个斜杠命令是通过相对路径找到主 skill 的。

装好后，在 Chat 里输入 `/` 应该能看到 `build-eval`、`hillclimb`、`eval-audit`、`eval-hillclimb`。Agent Skills 默认是开启的（`chat.useAgentSkills`，1.109 起默认 `true`）。

## 推荐设置

建 eval（`/build-eval`）时不需要改任何设置。

如果要让 hillclimb **无人值守地连跑多轮**，需要在项目的 `.vscode/settings.json` 里加下面几项（路径和命令换成你自己的）：

```jsonc
{
  // 1. 只自动批准这几条评测命令：用锚定的正则，不要写成 "node" 或 "python" 这种宽泛前缀。
  //    这条规则就是整个循环的安全边界。不要让它匹配 --approve-harness，那个参数只能由你本人手动执行。
  "chat.tools.terminal.autoApprove": {
    "/^node eval\\/run-eval\\.mjs --flow \\.hillclimb\\/my-flow --variant (baseline|v[0-9]+)( --(reps|limit|concurrency|timeout-s) [0-9]+| --model [A-Za-z0-9._@:-]+| --dry-run)*$/": { "approve": true, "matchCommandLine": true }
  },
  // 2. 每轮大约要几十次请求，默认上限不够跑多轮。
  "chat.agent.maxRequests": 200,
  // 3. 不让工作区搜索命中测试集的对话记录，避免数据泄漏进 agent 的上下文。
  "search.exclude": { "**/.hillclimb/**/traces/**": true, "**/.hillclimb/**/ref/**": true }
}
```

另外几点：

- **报告生成器和 wait-run 也要加自动批准规则。** 示例见 `references/vscode-copilot.md`。
- **保护 skill 自身的脚本。** VS Code 默认不保护 `.github/skills/**`，agent 改动这些脚本时不会询问。要么用安装方式 B，要么把 `"**/.github/skills/eval-hillclimb/**": false` 加进 `chat.tools.edits.autoApprove`；加完之后到设置界面确认内置的保护规则（`.vscode/*.json`、`.env` 等）还在。
- **公司策略禁用了终端自动批准时**，每轮都要手动点一次"允许"。这种情况下选"每轮确认一次"的节奏就好。
- **不要开** Autopilot、YOLO（`chat.tools.global.autoApprove`）或 `chat.autoReply`。它们会自动替你回答问题，而几个关键确认（输入集、打分方式、花钱）必须由你本人回复。

## 使用示例

```
/build-eval 客服工单分类器（src/triage/classify.py 里的 classify_ticket）
```

Copilot 会依次：

1. 确认要评估的流程和入口函数；
2. 问你测试用例从哪来（生产日志、bug 单、你手写，或者它根据代码合成）；
3. 列出所有输入请你确认；
4. 提议打分方式，先跑几个样例请你确认；
5. 在你的仓库里生成 runner 脚本；
6. 报出实测成本和耗时，等你同意后再跑全量；
7. 给出 `.hillclimb/<flow>/report.html`。

报告用浏览器打开；状态表 `trajectory/status.md` 可以在 VS Code 的 markdown 预览里看。

```
/hillclimb 提高 classify_ticket 的准确率，系统提示词可以改，输出 JSON 格式不能动
```

开始前它会先做几件事：

- 检查 eval 能不能真正分辨出改进：噪声底多大、还有多少提升空间；
- 和你确认目标、可以改什么、禁区、停止条件和预算；
- 划分训练集和测试集。

之后每轮的流程是：只读分析器 `eval-analyzer` 只看训练集的对话记录（由 `analyzer-slice` 脚本切出，主 agent 自己不读任何对话记录），提出一个改动 → 应用改动 → 跑全量 → 重新生成报告，并在聊天里贴出状态表（测试集和训练集分数、置信区间、相对基线的配对差异、延迟、token、$/run、累计花费）。

## 模型怎么选

| 角色 | 建议 |
|---|---|
| **主 agent**（Copilot 模型选择器里选的那个） | 这几份指南很长，选推理能力强的模型（Claude Sonnet/Opus 系列、GPT-5 级别）；小模型容易跳步骤。 |
| **分析器** | 可以在 `eval-analyzer.agent.md` 的 frontmatter 加一行 `model: "<模型名> (copilot)"`，用更强的模型做诊断。注意子 agent 不能用比主 agent 更贵一档的模型。 |
| **被测模型和 judge** | 由你的应用和 runner 脚本决定，和 Copilot 用哪个模型无关。 |

## 与原版的差异

| 原版（Claude Code） | 本移植（VS Code Copilot） |
|---|---|
| `/claude-api build-eval`、`/claude-api hillclimb` | `/build-eval`、`/hillclimb`、`/eval-audit`，或 `/eval-hillclimb <子命令>` |
| `AskUserQuestion` | `vscode/askQuestions`（推荐项标 `recommended`）；工具不可用时退化为在聊天里提问 |
| `Bash run_in_background` + 完成通知 | 终端 sync 调用加 `timeout` 在当前轮内等待；超时或耗时很长时改用 async，再配合 `wait-run` 脚本确认结束 |
| Task 子 agent（分析器） | `runSubagent` + 只读自定义 agent `eval-analyzer`（工具只有 `read/readFile`） |
| 会话级命令白名单 | `chat.tools.terminal.autoApprove` 锚定正则 |
| `.claude/hillclimb/<flow>/` | `.hillclimb/<flow>/` |
| 只有 Node 版 runner 模板和 lite 报告 | Node + Python 双版本；两个报告生成器输出逐字节一致 |
| 成本由完整版查看器或模型手算 | 报告生成器按 `_state.json.prices` 自动计价，生成 `trajectory/status.md`：含 95% 置信区间、配对差异、$/run、累计花费；缺价格时写 "not measured"，不会写成 $0 |
| 价格、judge 模型只针对 Claude | 不绑定供应商：`normalizeUsage` 兼容 OpenAI / Gemini 的用量格式，`modelMatches` 可以按部署名或网关别名自定义 |
| — | runner 新增 `--limit` / `--ids`（试跑和金丝雀用）、`--dry-run`（只打印范围、不花钱）、"同一 variant 已有一轮在跑"时拒绝启动、`progress.txt` 结束标记 |
| 完整版 HTML 查看器（`build-report.mjs`，上游本来就不随 skill 分发） | 不包含。用 lite 报告 + `status.md`，评分样例写成 `pilot-review.md` |

四份指南基本保留原文，只改了平台相关的段落，并在文件开头注明是移植版。上游更新时可以直接用 diff 对比后合并。

## 已知限制

- **Prompt files 已被 VS Code 标记为弃用**，所以斜杠命令用 skill 实现，不用 `.prompt.md`。
- **VS Code 的新 "Copilot / Agent Host" harness** 里，`runSubagent`、`askQuestions` 这些工具的名字可能不同。skill 写了回退方案：没有子 agent 时，会先征得你同意再继续。
- **从 Python 后台线程跑的同步调用**，超时后线程还会继续运行，直到底层请求结束。这一点和原版 Node 一样：超时只回收并发槽位，不能中止请求。

## 自测

```bash
python tests/run_tests.py
```

自测会用一个假应用把两个 runner 模板完整跑一遍，覆盖：harness 闸门、试跑、断点续跑、429 退避、超时、模型被替换、截断、冻结参照、并发保护、wait-run。然后在多组边界数据上对比 Node 和 Python 报告生成器的输出，要求逐字节一致。没有 Node 时会跳过 Node 部分。

## 许可

Apache-2.0。本目录衍生自 [anthropics/skills](https://github.com/anthropics/skills) 中 `skills/claude-api` 的 evals 部分，修改说明见 `NOTICE.md`。
