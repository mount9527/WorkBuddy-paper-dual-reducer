# 流程 · 生成任务并完成第一批改写

用户要的是「真的改」，从 `plan` 走到 `apply`。先读 SKILL.md 的「改写铁律」。

## 触发说法

「帮我改」「降 AIGC 味」「降重」「按段改一下」

## 定模式

| 用户说法 | `--scope` |
|---|---|
| 降 AIGC 味 / 去 AI 味 / 润色 | `aigc` |
| 降重 / 降重复率 | `dedupe` |
| 双降 / 两个都要 | `both` |

没说清时按关键词判断；不要为了「更全面」替用户加码。

## 步骤

1. 确认已诊断过；没有就先跑 `diagnose`（见 `flows/diagnose.md`）。

2. 生成任务：

```bash
python "<skill>/host/paperdual.py" plan --scope aigc --json
```

3. 取第一批提示词（4–6 段）：

```bash
python "<skill>/host/paperdual.py" prompts --json --limit 6 --skip 0
```

4. 按每条 `prompt` 改写，输出一行一个 `{"pid","text","op"}`，用 `Write` 追加到返回的 `editsPath`。
   不要改 docx，不要新增数字/引用/主观表述，字数照 `minChars~maxChars`。

5. 本批写完后回填：

```bash
python "<skill>/host/paperdual.py" apply --json
```

6. 用中文汇报：任务/提示词条数、预检可通过与被拒、结构复检、AIGC 复检（未装依赖就说明未执行）、
   是否回退、改稿路径。被拒项要给出 `reason` 与 `hint`，不要只说「失败了」。

## 异常

- `plan` 返回 `E_NOTHING`：这篇稿子已经没有需要改的段落，直接告诉用户，不要硬改。
- `plan` 返回 `E_LIMIT`：已到五改上限，停止并说明。
- 段数超过 12：改用子代理批量改写，模板见 `references/rewriter-prompt.md`。
