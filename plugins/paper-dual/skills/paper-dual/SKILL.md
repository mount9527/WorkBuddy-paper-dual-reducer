---
name: paper-dual
description: Diagnose and rewrite Chinese academic papers (.doc/.docx) to lower AI-style statistical fingerprints and in-document repetition, with byte-level Word formatting fidelity. Use when the user asks to 降 AIGC 味 / 去 AI 味 / 降重 / 降重复率 / 润色论文但保留原格式, or asks which paragraphs read like AI wrote them.
description_zh: 诊断并定向改写中文论文 .doc/.docx，降低 AI 写作痕迹与篇内重复，保留表格、公式、引用与 Word 格式。适用于降 AIGC 味、降重、按段改写并回填，以及在本机生成可预览的工作台。
description_en: Diagnose and rewrite Chinese academic .docx files to reduce AI-style fingerprints and in-document repetition, with formatting fidelity. Use for de-AI rewriting, repetition reduction, section-level revision, and a local workbench.
version: "1.0.0"
---

# 论文双降（降 AIGC 味 / 降篇内重复）

读一篇中文论文，诊断哪些段落「机器味重」或「自己抄自己」，按段生成改写任务，
把模型写的改稿**无损回填**到原文档，再用同一套闸门复检。改写只动表达层，不动事实、数据、引用、格式。

**一条硬边界**：模型永远不接触 docx。它只读 `prompts.jsonl`，只写 `edits.jsonl`；
解析、调度、回填、回滚、格式校验全在脚本里。这条边界决定了整件事能不能调试、能不能回退。

## 什么时候用

- 「降 AIGC 率」「去 AI 味」「让这段读起来像人写的」
- 「降重」「降重复率」「这段和前面重复了」
- 润色论文表达，但明确要求**保留原格式**
- 手里有一篇 `.docx`，问「哪里像 AI 写的」

## 什么时候不用

- **不承诺检测结果**。输出是篇内相对风险分，不是任何平台的检测概率。用户问「能降到多少」「能过知网吗」，
  只能回答：无法预测，本工具只做表达层优化。
- 不接「代写」「补数据」「编文献」。改写必须逐句可追溯回原文。
- 不做全文查重（无外部语料授权），只做篇内重复。
- 不支持 PDF、扫描件、图片；`.doc` 会自动转 `.docx` 副本，原文件不动。
- 公式段、表格、文本框、脚注、超链接、修订痕迹、代码块/目录树一律不改——这是设计，不是缺陷。

## 找到 CLI

本文件位于 `<plugin>/skills/paper-dual/SKILL.md`，宿主 CLI 在 `<plugin>/host/paperdual.py`。
先解析出**绝对路径**再调用，Windows 路径要加引号：

```bash
python "<plugin>/host/paperdual.py" doctor --json
```

解释器优先用 `python`；没有就试 `py -3`。需要 Python ≥ 3.10，引擎零第三方依赖。
所有命令都支持 `--json`，**调用时一律加 `--json`**，按返回的 `ok` / `error` / `code` 判断，不要靠退出码猜。

## 标准流程

### 0. 自检（每个会话一次）

```bash
python "<plugin>/host/paperdual.py" doctor --json
```

`AIGC 真机依赖` 为 false 不影响诊断、改写、回填；只影响 AI 疑似占比。**不要**因为这一项失败就停下。

### 1. 诊断（只读原文件）

```bash
python "<plugin>/host/paperdual.py" diagnose --in "<论文绝对路径>" --json
```

返回风险分布、可改写/锁定段数、篇内重复率、Top 12 段落、报告路径。
同时生成本机状态：`~/.workbuddy/paper-dual/docs/<文件名>-<哈希>/`。

诊断后立刻生成工作台并展示给用户：

```bash
python "<plugin>/host/paperdual.py" workbench --json
```

把返回的 `path` 用 `present_files` 打开。静态工作台只读，够用；需要点按钮操作时才用 `serve`。

### 2. 选模式

| 用户说法 | `--scope` | 说明 |
|---|---|---|
| 降 AIGC 味 / 去 AI 味 | `aigc`（默认） | 9 维痕迹 → 写进每段提示词 |
| 降重 / 降重复率 | `dedupe` | 13 字指纹找篇内重复 |
| 双降 / 两个都要 | `both` | 一次改写同时注入两类要求 |

用户没说清时按关键词判断；两种都提就用 `both`。不要为了「更全面」替用户加码。

### 3. 生成改写任务

```bash
python "<plugin>/host/paperdual.py" plan --scope aigc --json
```

返回 `versionId`（v1）、`tasks`、`prompts`、`promptsPath`、`editsPath`、`instruction`、`hints`、`focus`。
最多五改：`v1 改稿 → v2 二改 → … → v5 五改`；到达上限后 `plan` 会返回 `E_LIMIT`。

### 4. 改写（模型这一环）

分批取提示词，每批 4–6 段：

```bash
python "<plugin>/host/paperdual.py" prompts --json --limit 6 --skip 0
```

返回的 `system` 是共享系统提示，`rows[]` 每项有 `pid`、`op`、`opName`、`minChars`、`maxChars`、`sentences`、`prompt`。
对每一条：

1. 严格按 `prompt` 里的原文、约束、文风五条改写；
2. 输出**一行一个 JSON**：`{"pid": "...", "text": "改写后的整段文字", "op": "O2"}`；
3. 用 `Write` 工具把这一批写进 `editsPath`（不存在就新建；多批就追加，不要覆盖前一批）；
4. 全部写完后跑第 5 步。

**改写铁律**（违反会被闸门整段拒，且只废这一段）：

- 不新增数字、年份、百分比、引用、书名、英文缩写；原有实体必须原样保留；
- 不新增「我觉得／我认为／笔者认为」这类主观表述；
- 字数 ∈ 原文 × [0.55, 1.40]；提示词里给了 `minChars~maxChars`，照它写；
- 标题、表格、代码块、公式、脚注、参考文献不动；
- 不改变论断方向与结论强度；
- 一段只写一条，`pid` 不能重复，不能写不属于本轮基础稿的 `pid`。

### 5. 回填 + 复检

```bash
python "<plugin>/host/paperdual.py" apply --json
```

它会：预检 → 回填 → 篇级格式/实体校验 → 篇内重复率复检 →（依赖就绪时）冻结块 AIGC 复检 → 不通过则回退。

返回要点：

| 字段 | 含义 |
|---|---|
| `lint` | `would_apply` / `would_reject`，被拒项看 `rejected[].reason` 与 `hint` |
| `verdict.passed` | 结构复检（格式/实体/锁定区/结构）是否通过 |
| `acceptance.accepted` | 全部验收项是否通过 |
| `acceptance.aigcChecked` | 是否真的跑了 AIGC 复检（没装依赖时是 false） |
| `prob` | 正文 AI 疑似占比 before/after（仅真机） |
| `dup` | 篇内重复率 before/after |
| `rolledBack` / `rolledBackTo` | 是否回退、回到哪一版 |

`rolledBack: true` 时不要粉饰：直接告诉用户哪一项没过、被拒原因是什么、本版保留但不作为当前稿。

### 6. 展示结果

```bash
python "<plugin>/host/paperdual.py" compare --json        # 三栏对照数据
python "<plugin>/host/paperdual.py" workbench --json      # 重新生成工作台
python "<plugin>/host/paperdual.py" export --version v1 --out "<目标.docx>" --json
```

用 `present_files` 打开工作台和导出的 docx。工作台里有概览、AIGC 考官、任务与清单、三栏对照、报告五屏。

## 多轮续改

- 改稿未达标、用户想继续：`plan --base v1 --scope aigc` 生成 v2（`--base` 省略时用当前版本）。
- 第三轮起自动转「少量高风险段定向续改」：任务上限递减为 5/3/2，提示词只保留前 6 条最关键要求。
- 达到 v5 后不能再生成；只能重新诊断或换文件。

## 常见失败与处理

| `code` / 现象 | 原因 | 处理 |
|---|---|---|
| `E_STALE` | 诊断后原文件被改过 | `diagnose --in ... --force` 重新诊断，再重来 |
| `E_NO_EDITS` | 还没写 `edits.jsonl` | 按第 4 步写清单，再 `apply` |
| `E_FOREIGN_EDITS` | 清单里的 pid 不属于当前基础稿 | 多半贴了上一篇的清单；重新 `prompts` 生成 |
| `E_LIMIT` | 已到五改上限 | 停止续改，说明上限 |
| `E_NOTHING` | 9 维与逐段考官都没发现要改的段 | 说明这篇稿子痕迹已经很低，不要硬改 |
| `E_CONVERT` | `.doc` 转不了 `.docx` | 让用户先在 Word 里另存为 `.docx` |
| `E_BASE` | 基础版还没有 docx | 先 `apply` 出上一版，再续改 |
| `AIGC_DEPS_MISSING` | 没装 torch + transformers | 给安装步骤，**不要编分数**，可先按结构验收走 |
| `engine 缺失` | 插件安装不完整 | 重新安装插件，不要改引擎源码 |

## AIGC 考官（可选）

```bash
python "<plugin>/host/paperdual.py" aigc --json               # 占比 + 9 维
python "<plugin>/host/paperdual.py" aigc --calibrate --json   # 首次方向标定
```

没装依赖时返回 `AIGC_DEPS_MISSING` 和 `setup.installScript`（PowerShell，含国内镜像）。
装好后：

```bash
python "<plugin>/host/paperdual.py" config --python "E:\paper-dual-aigc\Scripts\python.exe" --hf-cache "E:\paper-dual-aigc\hf-cache"
```

首次打分/标定会联网下载约 409MB 权重（不随插件分发）。**任何情况下都不要展示或编造未经模型计算的分数。**

## 工作台

```bash
python "<plugin>/host/paperdual.py" workbench --json    # 静态 HTML，present_files 打开
python "<plugin>/host/paperdual.py" serve --port 8760   # 本地服务，可点按钮预检/回填
```

`serve` 只监听 `127.0.0.1`，不开远端。静态版是只读快照；需要交互时再用 `serve`，
把 `http://127.0.0.1:<port>/` 交给用户。服务进程随会话结束可能退出，退出就重开，不要紧。

## 口径与红线

- 风险分是**篇内相对指标**，只用于同篇排序，跨文档不可比，**不是任何平台的检测概率**。
- AIGC 考官给的是**本工具自有口径下的正文 AI 疑似占比**，不等于知网/维普/格子达的判定，也不与任何第三方平台横比。
- 不模拟任何平台，不承诺通过率。
- 标题、表格、代码块、公式、脚注、参考文献一律不改；不新增数字/引用/主观表述。
- 改稿必须由作者复核。

## 参考

- `references/gates.md`：六道闸门与验收矩阵（什么时候会被拒、怎么改）
- `references/operators.md`：九个改写算子的适用条件与风险
- `references/thresholds.md`：全部阈值与取值理由
- `references/compliance.md`：红线与交付话术
- `engine/`：解析/诊断/排期/提示词/回填/复检引擎（MIT）
- `aigc/`：AIGC 考官与 9 维诊断（MIT；模型权重不随插件分发）
