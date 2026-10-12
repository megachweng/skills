# Eval 构建 + Hill-climb：VS Code Copilot 版

把 Anthropic 官方 `claude-api` skill 里的评测（eval）功能单独提取出来，移植到 **VS Code 的 GitHub Copilot agent 模式**。提取的部分包括 `build-eval`、`hillclimb`、eval 体检和成本优化，上游源码在本仓库的 `skills/claude-api/shared/evals/`。

**专门照顾"公司电脑只允许在 VS Code 里用 Copilot"的场景：**

- 被测对象和裁判（judge）都以 **Copilot 子 agent** 的形式运行。
- 不需要任何 LLM API key，也不需要能联网调用模型的脚本。
- 不依赖 Claude Code、Copilot CLI 或 Codex CLI。

## 能做什么

| 命令 | 作用 |
|---|---|
| `/build-eval <想评估的功能>` | 访谈式地建一套评测，产出：你确认过的输入集、你确认过的打分方式、可重复运行的配置、基线报告 |
| `/hillclimb <要改进什么>` | 在已有 eval 上迭代改进。每轮流程：分析 → 改一处 → 全量重跑 → 记录。训练集和测试集分开，最终以测试集上的提升作为结论 |
| `/eval-audit <eval 在哪>` | 给已有 eval 做体检，检查它的数字是否可信 |
| `/eval-hillclimb cost ...` | 目标是"质量不降、成本降低"的 hillclimb |

能评估两类对象：

1. **Copilot 定制本身**：自定义 agent（`.agent.md`）、skill、`copilot-instructions.md` / `AGENTS.md` 等指令文件、prompt 文件，看它们在 Copilot 里的实际表现。
2. **产品里的 prompt**：将来会部署到别处（调 API）的系统提示词。在公司电脑上只能借 Copilot 的模型来测，属于近似测量（见"已知限制"）。

## 两种 runner

| | Copilot runner（默认，适合公司电脑） | 脚本 runner（原版方式） |
|---|---|---|
| 被测对象 / 裁判怎么跑 | `runSubagent` 调用被测 agent 和 `eval-judge` | 本地脚本直接调用应用的 LLM API |
| 需要的东西 | 只要 VS Code + Copilot；本地 Python 只做记账、打分和出报告，不联网 | 能从脚本调用的 API 通道 |
| 能测量的东西 | 分数、置信区间、子 agent 调用次数、耗时 | 再加上 token、美元成本、延迟，并能校验实际服务的模型 |

两种 runner 写出的文件格式相同，后面的报告、hillclimb 流程完全一样。

## 目录结构

```
copilot-eval-hillclimb/
├── .github/
│   ├── skills/
│   │   ├── eval-hillclimb/              # 主 skill
│   │   │   ├── SKILL.md
│   │   │   ├── references/              # 指南：build-eval / eval-hillclimb / eval-audit / cost-hillclimb
│   │   │   │   ├── copilot-runner.md    #   Copilot runner 协议（重点）
│   │   │   │   ├── vscode-copilot.md    #   VS Code 平台适配
│   │   │   │   ├── data-contract.md     #   文件格式
│   │   │   │   ├── pricing.md
│   │   │   │   └── templates/eval-sut.agent.md   # 产品 prompt 的被测 agent 模板
│   │   │   └── scripts/
│   │   │       ├── copilot_eval.py      # Copilot runner 记账：plan / batch / judge-prep / record / regrade / progress
│   │   │       ├── vscode_settings.py   # 按真实安装路径生成 VS Code 设置（自动批准规则）
│   │   │       ├── build-report-lite.mjs / build_report_lite.py   # 报告（两版输出逐字节一致）
│   │   │       ├── analyzer-slice.mjs / analyzer_slice.py         # 只切出训练集给分析器
│   │   │       ├── runner-scaffold.mjs / runner_scaffold.py       # 脚本 runner 模板
│   │   │       └── wait-run.mjs / wait_run.py                     # 脚本 runner 等待助手
│   │   ├── build-eval/  hillclimb/  eval-audit/   # 三个斜杠命令（薄包装）
│   └── agents/
│       ├── eval-case-worker.agent.md    # 每批用例的执行者：调被测对象和裁判，落盘，只回传分数
│       ├── eval-judge.agent.md          # 裁判：只读，输出一个 JSON 判定
│       └── eval-analyzer.agent.md       # hillclimb 分析器：只读训练集
├── tests/run_tests.py
└── README.md / NOTICE.md / LICENSE.txt
```

## 前置条件

- **VS Code 1.110 或更新**，按 1.141 核对过；Copilot Chat 用 agent 模式。
- **Python 3.8+**，Node 可选。脚本只用标准库，不用 `pip install` 或 `npm install`。
- **Copilot 模型选择器里要有可用的模型。** 子 agent 不能使用比主会话模型更贵一档的模型，所以主会话要选够强的模型。

## 安装

两种方式任选：

- **A. 装到项目里**：团队共享，可以随仓库提交。
- **B. 装到用户目录**：只对自己生效，并且 skill 文件在工作区之外，agent 要改它们时 VS Code 会先询问。

PowerShell：

```powershell
$src = "<本仓库路径>\copilot-eval-hillclimb\.github"
# A. 装到项目
Copy-Item -Recurse "$src\skills\*" "<你的项目>\.github\skills\"
Copy-Item "$src\agents\*.agent.md" "<你的项目>\.github\agents\"
# B. 装到用户目录
New-Item -ItemType Directory -Force "$HOME\.copilot\skills", "$HOME\.copilot\agents" | Out-Null
Copy-Item -Recurse "$src\skills\*" "$HOME\.copilot\skills\"
Copy-Item "$src\agents\*.agent.md" "$HOME\.copilot\agents\"
```

bash：

```bash
src=<本仓库路径>/copilot-eval-hillclimb/.github
mkdir -p ~/.copilot/skills ~/.copilot/agents          # 或 <你的项目>/.github/skills、.github/agents
cp -R "$src"/skills/* ~/.copilot/skills/ && cp "$src"/agents/*.agent.md ~/.copilot/agents/
```

四个 skill 文件夹必须放在同一个 `skills/` 目录下，因为三个斜杠命令是通过相对路径找到主 skill 的。

装好后，在 Chat 里输入 `/` 应该能看到 `build-eval`、`hillclimb`、`eval-audit`、`eval-hillclimb`。

## 首次使用要做的设置

1. **生成设置。** 设置不用手写，让 Copilot 运行（或者你自己在仓库根目录运行）：

   ```
   python "<skill 目录>/scripts/vscode_settings.py" --flow .hillclimb/<flow名> --runner copilot
   ```

   它会按你机器上的真实路径，打印一段 JSON 和对应的命令格式。你检查后把 JSON 合并进 `.vscode/settings.json`。里面包括：

   | 设置 | 作用 |
   |---|---|
   | `chat.subagents.allowInvocationsFromSubagents: true` | 允许 worker 子 agent 再去调用被测对象和裁判 |
   | `chat.tools.terminal.autoApprove` | 只放行这几个评测脚本、这个 flow 的命令。正则锚定到绝对路径，匹配不到 `--approve-harness`、`;`、`&&` 或管道。**这些规则就是无人值守循环的安全边界** |
   | `chat.agent.maxRequests: 200` | 默认上限不够跑多轮 |
   | `search.exclude: **/.hillclimb/**` | 防止工作区搜索把测试集内容带进 agent 的上下文 |

2. **批准 harness。** 用例、打分器和评分标准定稿之后，由你本人在仓库根目录跑一次：

   ```
   python "<skill 目录>/scripts/copilot_eval.py" plan --flow .hillclimb/<flow名> --variant baseline --dry-run --approve-harness
   ```

   它只记录 harness 的 sha，不会开始跑。以后打分器、用例或评分标准一改，所有命令都会拒绝执行，直到你看过改动、再跑一次这条命令。

另外几点：

- **保护 skill 自身的脚本。** VS Code 默认不保护 `.github/skills/**`，agent 改动这些脚本时不会询问。用安装方式 B 就能避开这个问题。
- **不要开** Autopilot、YOLO 或 `chat.autoReply`。它们会自动替你回答问题，而几个关键确认（输入集、打分方式、花费）必须由你本人回复。
- **公司策略禁止了终端自动批准时**，每条命令都要手动点"允许"。这种情况下只能逐轮确认地跑。

## 使用示例

**评估一个产品 prompt：**

```
/build-eval 我们客服工单分类的系统提示词（prompts/triage.md），想在 GPT-5 mini 上测准确率
```

Copilot 会依次：

1. 确认用 Copilot runner；
2. 用 `templates/eval-sut.agent.md` 生成一个无工具的被测 agent，把你的 prompt 放进去；
3. 和你一起整理用例；
4. 选打分方式：能用程序判断的优先用程序（`eval/grade.py`），需要主观判断的用 `eval-judge` 按评分标准打；
5. 先试跑 3 个用例，生成 `pilot-review.md` 请你确认打分是否合理；
6. 报出"N 个用例 × R 次重复 = X 次被测调用 + Y 次裁判调用，约 Z 分钟"，等你同意后再跑全量；
7. 给出 `.hillclimb/<flow>/report.html`（用浏览器打开）和 `trajectory/status.md`。

**评估一个 Copilot 自定义 agent 或 skill：** 方式相同，被测对象直接用你现有的 agent。对 skill 和指令文件，它会先做一个"开关对照"，确认子 agent 确实加载了它们，再开始评测。

**迭代改进：**

```
/hillclimb 提高分类准确率，只能改 prompts/triage.md，输出 JSON 格式不能动
```

开始前会先确认几件事：

- eval 能不能真正分辨出改进：噪声底多大、还有多少提升空间；
- 目标、可以改什么、禁区、停止条件和预算（按调用次数和时间算）；
- 训练集和测试集的划分。

之后每轮的流程是：

1. `eval-analyzer` 只读训练集的对话记录（由 `analyzer-slice` 切出），提出一个改动；
2. 应用改动；
3. 多个 `eval-case-worker` 并行把全量跑完；
4. 生成状态表，包含测试集和训练集分数、95% 置信区间、相对基线的配对差异、调用次数。

主 agent 全程看不到任何一条回复的原文，测试集的隔离靠这一点保证。

## 已知限制（Copilot runner）

- **测产品 prompt 是近似测量。** Copilot 会在子 agent 外面套一层自己的系统提示，所以绝对分数只是生产环境的近似值。同一模型、同一 VS Code 版本下，不同版本之间的**比较**才是可靠的信号。
- **拿不到 token 用量、美元成本、单例延迟**，也无法校验实际是哪个模型服务的这次调用。成本只能用子 agent 调用次数和耗时来衡量；这些调用怎么计入你的 Copilot 配额，取决于你们的订阅方案。
- **只能拿到被测对象的最终回复**，看不到它中间的工具调用。对会改文件的 agent，要在 fixture 目录里用程序检查它留下的最终状态。
- **回复由 worker 抄写到磁盘。** 非常长的回复可能被截断。打分器可以检查回复的结尾结构，批次也应该小一点。
- **VS Code 升级可能改变 Copilot 的内部提示。** 循环中途升级后要重新跑基线。
- **新的 Copilot / Agent Host harness 里，工具名可能不同**，skill 写了回退方案。

如果以后确认公司允许安装自己打包的 VS Code 扩展（VSIX），可以再加一个基于 `vscode.lm` API 的批量 runner：它能直接调用 Copilot 模型、并发更高，也省掉抄写这一步。

## 与原版的差异（摘要）

| 原版（Claude Code） | 本移植 |
|---|---|
| `/claude-api build-eval` 等子命令 | `/build-eval`、`/hillclimb`、`/eval-audit` |
| 脚本直接调 LLM API | 默认改为 Copilot 子 agent 执行；原来的脚本 runner 保留为可选 |
| `AskUserQuestion` / Task 子 agent / 后台 Bash | `vscode/askQuestions` / `runSubagent` + 自定义 agent / 终端 sync·async + `wait-run` |
| 会话级命令白名单 | `vscode_settings.py` 生成的锚定 `chat.tools.terminal.autoApprove` 规则 |
| 只有 Node 版 | 主要脚本都有 Node + Python 双版本，报告输出逐字节一致 |
| 成本由完整版查看器或模型手算 | 报告生成器自动计算（美元或调用次数），写进 `trajectory/status.md` |
| — | 新增 `analyzer-slice`（主 agent 不碰测试集）、`--dry-run` / `--limit` / `--ids`、运行中保护、卡死检测 |

四份指南基本保留原文，只改了平台相关段落，并在开头注明是移植版，方便以后和上游 diff 合并。完整的改动清单见 `NOTICE.md`。

## 自测

```bash
python tests/run_tests.py
```

自测覆盖：

- 两个脚本 runner 模板：harness 闸门、试跑、断点续跑、429 退避、超时、模型被替换、截断、冻结参照；
- Copilot runner：用模拟 worker 走完分批、裁判、成对比较的 A/B 回写、重新打分、闸门；
- 生成的自动批准正则：用真实的 JS 正则引擎测试该放行和该拒绝的命令；
- Node 与 Python 报告生成器：多组边界数据下输出逐字节一致；
- wait-run 的卡死检测。

## 许可

Apache-2.0。衍生自 [anthropics/skills](https://github.com/anthropics/skills) 中 `skills/claude-api` 的 evals 部分，修改说明见 `NOTICE.md`。
