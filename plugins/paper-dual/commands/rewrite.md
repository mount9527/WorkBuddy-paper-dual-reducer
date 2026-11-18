---
description: "为已诊断的论文生成改写任务，并完成第一批逐段改写"
argument-hint: "[aigc|dedupe|both] [要改几段]"
---

为当前论文生成改写任务，并完成第一批改写。参数：`$ARGUMENTS`（模式与段数，可省略）。

先读 `skills/paper-dual/SKILL.md` 的「标准流程」与「改写铁律」，再执行。

1. 模式：参数里出现 `dedupe`/降重 → `--scope dedupe`；出现 `both`/双降 → `--scope both`；
   其余默认 `--scope aigc`。

2. 生成任务：

```bash
python "<插件根>/host/paperdual.py" plan --scope aigc --json
```

3. 取第一批提示词（4–6 段）：

```bash
python "<插件根>/host/paperdual.py" prompts --json --limit 6 --skip 0
```

4. 按每条 `prompt` 改写，输出一行一个 `{"pid","text","op"}`，用 `Write` 追加到返回的 `editsPath`。
   不要改 docx，不要新增数字/引用/主观表述，字数照 `minChars~maxChars`。

5. 本批写完后回填：

```bash
python "<插件根>/host/paperdual.py" apply --json
```

6. 用中文汇报：任务/提示词条数、预检可通过与被拒、结构复检、AIGC 复检（未装依赖就说明未执行）、
   是否回退、改稿路径。被拒项要给出 `reason` 与 `hint`，不要只说「失败了」。

如果 `plan` 返回 `E_NOTHING`，说明这篇稿子已经没有需要改的段落，直接告诉用户，不要硬改。
