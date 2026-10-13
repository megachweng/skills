# 开发交接：eval-hillclimb 的 VS Code Copilot 移植

这份文档写给接手继续开发的人（包括本地的 Claude session）。它记录了这个目录是什么、为什么这样设计、现在做到哪一步、下一步做什么。面向最终用户的说明在 `README.md`，改动清单在 `NOTICE.md`。

## 1. 背景与约束

**目标**：把 Anthropic 官方 `claude-api` skill 中的 eval 功能单独提取出来，移植到 **VS Code 的 GitHub Copilot agent 模式**。具体包括 `build-eval`、`hillclimb`、`eval-audit`、`cost-hillclimb`，上游源码在本仓库的 `skills/claude-api/shared/evals/`。

**用户环境**（公司电脑）的约束，后面所有设计都围绕这几条：

| 约束 | 含义 |
|---|---|
| 不能用 Copilot CLI、Claude Code、Codex CLI | 只能在 VS Code 的 Copilot Chat（agent 模式）里干活 |
| **唯一允许的 LLM 通道是 VS Code 里的 Copilot 订阅** | 不能用脚本直接调 LLM API，被测对象和裁判都必须经过 Copilot |
| 可以运行本地 Node 和 Python 脚本 | 前提是脚本不联网、不调 LLM，只做打分、切分、出报告 |
| 不确定能否安装自建 VS Code 扩展（VSIX） | 所以默认方案不依赖扩展 |
| 主流模型都有（Claude、GPT、Gemini 等） | 驱动 skill 的主 agent 不一定是 Claude，文档不能依赖 Claude 专有行为 |

**评估对象有两类，都要支持：**

1. **Copilot 定制本身**：自定义 agent（`.agent.md`）、skill、`copilot-instructions.md` / `AGENTS.md` 等指令文件、prompt 文件。
2. **产品 prompt**：将来部署到别处调 API 的系统提示词。在公司电脑上只能借 Copilot 的模型来测，属于近似测量。

## 2. 当前状态

- **分支**：`claude/copilot-eval-hillclimb`，已推送到 `megachweng/skills`，还没有开 PR 到 `main`。
  - `8b709b6`：初版移植，脚本 runner 加 VS Code 平台适配。
  - `421dda6`：Copilot runner，修复第一轮审查的 12 个问题。
- **自测**：`python copilot-eval-hillclimb/tests/run_tests.py` 共 134 项检查全部通过。在 Python 3.11 / 3.12 / 3.13 和 Node 22 下跑过；没有 Node 时会跳过 Node 部分。
- **⚠️ 还没在真实 VS Code 里跑过。** 所有 VS Code 的工具名、frontmatter 字段和设置项，都是按 VS Code 1.141（2026-10）的 `microsoft/vscode-docs` 文档和 `release/1.141` 源码核对的。三个 agent 之间的真实调用（主 agent → worker → 被测对象 / judge）只用模拟 worker 测过脚本那一半。**这是下一步最重要的事**（见第 9 节）。

## 3. 目录结构与各文件职责

```
copilot-eval-hillclimb/
├── .github/
│   ├── skills/
│   │   ├── eval-hillclimb/              主 skill，所有逻辑在这里
│   │   │   ├── SKILL.md                 入口：子命令路由、开工前步骤（选 runner、选运行时）、硬性规则
│   │   │   ├── references/
│   │   │   │   ├── build-eval.md        上游指南（移植版）：访谈、两次确认、runner、试跑、基线报告
│   │   │   │   ├── eval-hillclimb.md    上游指南（移植版）：hillclimb 循环
│   │   │   │   ├── eval-audit.md        上游指南（移植版）：eval 体检清单
│   │   │   │   ├── cost-hillclimb.md    上游指南（移植版）：成本优化搜索（偏 Claude，开头有移植说明）
│   │   │   │   ├── copilot-runner.md    新增：Copilot runner 协议（被测对象类型、配置、安装设置、运行一轮、限制）
│   │   │   │   ├── vscode-copilot.md    新增：VS Code 平台适配（工具映射、脚本 runner 的运行与等待、设置、分析器）
│   │   │   │   ├── data-contract.md     新增：flow 目录的文件格式，以及报告里每个数字怎么算（取代上游 SCHEMA.md）
│   │   │   │   ├── pricing.md           新增：脚本 runner 的计价（_state.json.prices）
│   │   │   │   └── templates/eval-sut.agent.md   被测 agent 模板（测产品 prompt 用，不带任何工具）
│   │   │   └── scripts/
│   │   │       ├── copilot_eval.py      Copilot runner 的记账部分：plan / batch / judge-prep / record / regrade / progress（仅 Python）
│   │   │       ├── vscode_settings.py   按真实安装路径生成 VS Code 设置和锚定的自动批准正则（仅 Python）
│   │   │       ├── build-report-lite.mjs / build_report_lite.py   报告生成器，两版的 scores.tsv 和 status.md 必须逐字节一致
│   │   │       ├── analyzer-slice.mjs / analyzer_slice.py         只切出训练集给分析器，只打印数量
│   │   │       ├── runner-scaffold.mjs / runner_scaffold.py       脚本 runner 模板（调 API 的场景）
│   │   │       └── wait-run.mjs / wait_run.py                     脚本 runner 的等待助手
│   │   ├── build-eval/SKILL.md          /build-eval 斜杠命令（薄包装，disable-model-invocation）
│   │   ├── hillclimb/SKILL.md           /hillclimb
│   │   └── eval-audit/SKILL.md          /eval-audit
│   └── agents/
│       ├── eval-case-worker.agent.md    Copilot runner 的执行者：跑一批用例，只回传分数
│       ├── eval-judge.agent.md          裁判：只有 read/readFile，只输出一个 JSON
│       └── eval-analyzer.agent.md       hillclimb 分析器：只有 read/readFile，只读交给它的训练集文件
├── tests/run_tests.py                   端到端自测（只用标准库）
├── README.md                            给最终用户（中文）
├── NOTICE.md                            Apache-2.0 衍生声明和逐文件改动清单
├── LICENSE.txt
└── DEVELOPMENT.md                       本文件
```

## 4. 架构

### 4.1 两种 runner，同一份数据约定

| | Copilot runner（默认） | 脚本 runner（原版方式） |
|---|---|---|
| 被测对象 / judge | `runSubagent` 调用被测 agent 和 `eval-judge` | 本地脚本直接调用应用的 API |
| 记账 | `copilot_eval.py` | `runner-scaffold.*`，复制到用户仓库后填空 |
| 能测量的东西 | 分数、CI、子 agent 调用次数、耗时 | 再加上 token、美元、延迟，并能校验实际服务的模型 |
| `_state.json.runner` | `"copilot"` | 不写 |

两种 runner 写出的文件格式完全相同：

- 每个变体一个目录 `baseline/` 或 `v<N>/`，里面有 `results.jsonl`、`errors.jsonl`、`traces/<id>_rep<k>.json` 和 `progress.txt`；
- 基线输出冻结在 `baseline/ref/`，作为成对比较的参照。

所以报告生成器、`analyzer-slice` 和整个 hillclimb 流程，两种 runner 共用。格式细节见 `references/data-contract.md`。

### 4.2 Copilot runner 一轮的数据流

```
主 agent (Copilot Chat，模型由用户选)
  ├─ copilot_eval.py plan        → _work/plan.json，按批分组；同一用例的所有 rep 放在同一批
  ├─ runSubagent(eval-case-worker) × K 批，可在一轮里并行
  │     ├─ copilot_eval.py batch      → 任务单：每个用例的输入、回复要写到哪个文件
  │     ├─ runSubagent(被测 agent, prompt = 输入原文)  → 回复由 worker 原样写进 output.md（失败时写 error.txt）
  │     ├─ copilot_eval.py judge-prep → 裁判任务单（需要时才写 expected.md，记录后删除）
  │     ├─ runSubagent(eval-judge, prompt = 文件路径)  → 裁判读文件，回复由 worker 写进 judge.json
  │     └─ copilot_eval.py record     → 程序化打分 + 合并裁判分 → results.jsonl / traces / ref
  │        worker 只回传 "recorded q7 rep0: correct=1, quality=7" 这样的分数行
  ├─ copilot_eval.py progress    → 列出缺失项 → 重新 plan，只会补跑缺的
  └─ build_report_lite.py        → report.html、trajectory/scores.tsv、trajectory/status.md
```

hillclimb 时，每轮分析用的是另一条路径：`analyzer-slice` 只切出训练集的 `_analyzer/` 文件，交给 `eval-analyzer` 子 agent。分析器提出一个改动，主 agent 应用后再跑一轮。

### 4.3 关键设计决策及理由

1. **斜杠命令用 skill 实现，不用 prompt files。** VS Code 已把 prompt files 标为弃用，Agent Host harness 也不加载它们。三个包装 skill 用相对路径 `../eval-hillclimb/SKILL.md` 找到主 skill，所以四个 skill 文件夹必须放在同一个 `skills/` 目录下。
2. **用 worker 子 agent 嵌套调用被测对象和 judge**（需要 `chat.subagents.allowInvocationsFromSubagents: true`），原因有两个：
   - **测试集隔离**：主 agent 只看到分数，看不到任何回复原文。这对应上游的要求："the outer session does not read transcripts"。
   - **上下文容量**：几十个用例 × 若干 rep 的回复如果都进主 agent 上下文，会撑爆。

   嵌套关闭时的退路是主 agent 自己按 worker 协议跑。build-eval 试跑可以这样做；hillclimb 要先征得用户同意，因为测试集回复会进入主 agent 的上下文。
3. **`copilot_eval.py` 不调模型、不联网**，所以它的自动批准规则风险很低。它执行的打分器代码由 harness sha 锁定。
4. **标准答案（gold）放在工作区外。** 带文件工具的被测 agent 能读工作区里任何文件，所以 `expected` 不放进用例文件。`judge-prep` 只在某个用例的所有 rep 都有回复之后才写 `expected.md`，`record` 记录完就删掉。`plan` 会对放在工作区内的 gold 发出警告。
5. **成对比较的 A/B 顺序**按 `sha256(id|rep|variant)` 确定，裁判回复由 `record` 映射回候选者的 `win`（1 / 0.5 / 0）。基线自己的行固定为 `win = 0.5`。
6. **报告里的每个数字都由报告生成器从文件算出来**，写进 `trajectory/status.md`，内容包括 CI、配对差异、性能、$/run 或调用次数。主 agent 只引用、不手算。理由是：非 Claude 模型手算几百行数据不可靠，而且上游有"never type a number in"的原则。
7. **Node 和 Python 两版报告生成器必须逐字节一致。** 用户可能只有其中一种运行时。Python 版刻意模仿了 JavaScript 的几处语义：
   - 对象键顺序：整数样的键排在前面并按数值升序；
   - `String(number)` 的格式；
   - `toFixed` 的舍入：按精确的二进制值，四舍五入时远离零；
   - JS 的 `trim` 空白字符集；
   - 浮点求和从左到右逐项相加（Python 3.12+ 的 `sum()` 用补偿求和，结果会差最后一位）；
   - `NaN` / `Infinity` 不当作合法 JSON。

   **改任何一版，都要同步改另一版，并跑测试。**
8. **自动批准正则由 `vscode_settings.py` 生成**，不手写。规则锚定到脚本的绝对路径和单个 flow，只允许该脚本自己的参数，因此不会匹配 `--approve-harness`、`;`、`&&` 或管道。测试会用真实的 JS 正则引擎验证这一点。
9. **harness 闸门（`--approve-harness`）只由用户本人执行**，而且要带 `--dry-run`（只记录 sha，不开跑），在仓库根目录执行。sha 里的路径是相对当前目录计算的。
10. **`search.exclude: **/.hillclimb/**`**：防止 Copilot 的工作区搜索把测试集内容带进主 agent 的上下文。
11. **计价只认带日期后缀的快照名。** 例如 `claude-opus-5-5` 能匹配 `claude-opus-5-5-20260101`，但 `gpt-x` 不会匹配 `gpt-x-mini`。
12. **上游四份指南尽量保留原文**，只改平台相关的段落，每份开头都注明是移植版。这样上游更新时可以直接 diff 合并。

## 5. 依赖的 VS Code 能力（按 1.141 核对）

| 能力 | 用法 | 是否在真实环境验证过 |
|---|---|---|
| Agent Skills（`.github/skills/`、`~/.copilot/skills/`，`/skill-name` 调用） | 入口 | 否 |
| frontmatter：`name` 必须等于文件夹名；`description` 不超过 1024 字符；`argument-hint`、`disable-model-invocation`、`license` | SKILL.md | 否（名字不合法会**静默**加载失败） |
| `vscode/askQuestions`（核心工具，选项可以标 `recommended`） | 提问 | 否 |
| `runSubagent`，参数 `agentName`、`model`、`description`、`prompt` | worker、judge、分析器 | 否 |
| 嵌套子 agent：`chat.subagents.allowInvocationsFromSubagents`，默认 false，最大深度 5 | worker 调用被测对象和 judge | 否 |
| 自定义 agent frontmatter：`tools: ['read/readFile']`、`tools: []`、`agents: []`、`user-invocable: false` | 三个 agent 和被测 agent 模板 | 否 |
| `run_in_terminal`，参数 `mode: sync/async` 和 `timeout`；超时后命令转后台；静默超过 5 分钟也转后台 | 脚本 runner | 否 |
| `chat.tools.terminal.autoApprove`：`"/regex/"` 键，值为 `{approve, matchCommandLine}` | 无人值守 | 否（尤其是 Windows / PowerShell 下的引号和反斜杠） |
| `chat.agent.maxRequests`：文档写默认 25，源码回退值 50 | 多轮循环 | 否 |
| 子 agent 不能使用比主会话更高成本档的模型 | 选模型 | 否 |

另外两点：

- **默认的编辑保护**不覆盖 `.github/skills/**`，但覆盖 `.github/agents/**` 和 `.vscode/*.json`。
- **Agent Host 新架构**（Copilot harness）可能让工具名、prompt files 和 Local agent 的行为发生变化，Local agent 将来也可能被移除。

## 6. 测试

```bash
python copilot-eval-hillclimb/tests/run_tests.py
```

覆盖范围：

| 测试对象 | 覆盖内容 |
|---|---|
| 两个脚本 runner 模板（用假应用填空） | harness 闸门（拒绝 / 批准 / 改动后再拒绝）、`--dry-run`、`--limit`、`--ids`、断点续跑不重复、429 退避、超时、模型被替换、截断、路径安全 id、冻结参照、并发保护、`finished` 标记、`errors.jsonl` |
| `wait-run` | 结束返回 0、运行中返回 3、卡死返回 4、`--just-launched` 忽略残留的 `finished` |
| `copilot_eval.py`（模拟 worker 按任务单写回复和裁判结果） | 闸门、批次分组、程序化打分 + 裁判合并、裁判回复不可用、失败重排、成对比较的 A/B 回写、`regrade`、打分器改动触发闸门、`expected.md` 不残留 |
| `vscode_settings.py` | 生成的正则用 node 实测：该放行的放行；该拒绝的拒绝（`--approve-harness`、`;`、别的 flow、别处的同名脚本） |
| 报告生成器 | 多组合成数据下 Node 与 Python 的 `scores.tsv` 和 `status.md` 逐字节一致。数据覆盖：整数样 id、Unicode / emoji、BOM、CRLF、坏行、缺 usage、未定价模型、`m-a-mini` 前缀陷阱、无划分、copilot 模式、`summary.json` 的 suspicious |
| `analyzer-slice` | 只输出训练集，两版输出一致 |

## 7. 已知限制

- **测产品 prompt 是近似测量。** Copilot 会在子 agent 外再套一层系统提示，所以只有同一模型、同一 VS Code 版本下的变体**比较**可靠。
- **Copilot runner 拿不到这些信息：** token、美元、单例延迟、实际服务的模型、被测 agent 中间的工具调用、截断标志。
- **回复由 worker 模型抄写到磁盘**，超长回复可能被截断或改动。缓解办法是小批次，再让打分器检查回复的结尾结构。
- **会改文件的被测 agent** 只能检查它留下的最终状态，需要每个用例一个 fixture 目录。目前只在文档里说明了，没有工具支持。
- **`copilot_eval.py` 和 `vscode_settings.py` 只有 Python 版。** 用户确认过两种运行时都有。
- **`cost-hillclimb.md` 的很多量化结论是 Claude 专有的**，在 Copilot runner 下大多无法观测，开头的移植说明里写了这一点。

## 8. 与上游同步

上游四份指南在 `skills/claude-api/shared/evals/`，移植版在 `copilot-eval-hillclimb/.github/skills/eval-hillclimb/references/`。上游更新时：

1. 先 diff 上游新旧两个版本，找出上游改了什么；
2. 把这些改动合入移植版，平台相关段落保留移植写法；
3. 合入后搜一遍残留，不应出现：`.claude/`、`AskUserQuestion`、`full viewer`、`shared/`（`cost-hillclimb.md` 开头的说明除外）、`/claude-api`；
4. 如果上游改了 `runner-scaffold.mjs` 或 `build-report-lite.mjs`，要把改动同步到 Python 版，并跑一遍测试。

## 9. 下一步（按优先级）

1. **在公司电脑的 VS Code 里实测一遍**，这是最高优先级，按以下顺序：
   1. 安装后 `/` 菜单里能看到 4 个 skill，`eval-analyzer`、`eval-case-worker`、`eval-judge` 能被 `runSubagent` 按名字调用。
   2. 确认 `tools: ['read/readFile']` 和 `tools: []` 的实际效果：judge 能读文件、被测 agent 确实没有任何工具。
   3. 开启嵌套子 agent 后，worker 能调用被测 agent 和 judge；关闭时 worker 返回 `NESTED_SUBAGENTS_DISABLED`。
   4. 用 `vscode_settings.py` 生成的规则，在 **Windows / PowerShell** 下确认 worker 发出的命令（带引号的绝对路径）能被自动批准，`--approve-harness` 不会被自动批准。
   5. 用 3 个用例跑一次 `/build-eval` 试跑，检查：worker 抄写回复是否保真、`record` 打出的分数、`status.md` 里的 `calls/run`。
   6. 对 skill / 指令文件类的被测对象，确认子 agent 是否真的加载了它们（"开关对照"）。
   7. 记下实测发现的工具名或字段差异，回改 `vscode-copilot.md`、`copilot-runner.md` 和三个 agent 文件。
2. **根据实测修正文档和 agent 提示词**，尤其是 worker 的协议：它是整个链路里对模型能力最敏感的一环。
3. **（可选）抄写保真校验**：`record` 可以要求被测 agent 在回复末尾带一个固定的结束标记，或者让 worker 同时写出回复的字符数，用来发现截断。
4. **（可选，确认允许装 VSIX 之后）基于 `vscode.lm` 的 runner 扩展**：直接调用 Copilot 模型，可以批量并发，省掉 worker 抄写这一步。它应该写出同一份数据约定，这样报告和 hillclimb 都能复用。
5. **（可选）Agent Host / Copilot harness 兼容**：等它成为默认后，检查子 agent、提问工具和 skill 加载的差异。
6. **开 PR 合并到 `main`**，并决定是否把这个目录加进 `.claude-plugin/marketplace.json`。它不是 Claude Code 插件，可能不应该加。

## 10. 继续开发时的约定

- **改报告生成器**：两版同时改，并保持迭代顺序和浮点运算顺序一致；跑 `tests/run_tests.py`，确认逐字节一致的检查都通过。
- **改 `copilot_eval.py` 的输出格式**（任务单、record 的输出行）：同步改 `eval-case-worker.agent.md` 和 `copilot-runner.md`，并更新测试里解析任务单的正则。
- **加脚本参数**：同步改 `vscode_settings.py` 里对应的允许参数，否则自动批准会失效；在 `test_settings_rules` 里补测试。
- **skill 和 agent 文件**：`name` 必须等于文件夹名或文件名主干，`description` 不超过 1024 字符，YAML 不要放注释。加了新文件要更新 `NOTICE.md`；本目录是 Apache-2.0 衍生作品。
- **硬性规则不要放松**：`--approve-harness` 只由用户执行；三次确认（输入集、打分方式、花费）必须是用户亲口回复；主 agent 不读测试集内容；报告数字只来自生成器。
- **文档语言**：给模型读的文件（skill、references、agent）用英文，这样能和上游 diff，也最稳；给人读的 `README.md` 和本文件用中文。
