---
description: "诊断一篇中文论文的 AI 文风与篇内重复，生成本机工作台"
argument-hint: "[论文 .doc/.docx 的绝对路径]"
---

诊断这篇论文：`$ARGUMENTS`

步骤：

1. 先跑自检（每个会话一次即可）：

```bash
python "<插件根>/host/paperdual.py" doctor --json
```

插件根目录是本命令所在插件目录；CLI 在 `host/paperdual.py`，引擎在 `engine/`。
Windows 路径加引号。没有 `python` 就用 `py -3`。

2. 诊断（只读原文件，不改它）：

```bash
python "<插件根>/host/paperdual.py" diagnose --in "$ARGUMENTS" --json
```

如果 `$ARGUMENTS` 为空，向用户要论文的绝对路径，不要猜。

3. 生成工作台并打开：

```bash
python "<插件根>/host/paperdual.py" workbench --json
```

把返回的 `path` 用 `present_files` 展示。

4. 用中文向用户汇报（不要贴 JSON）：

- 可改写 / 锁定段数，风险分布高/中/低，篇内重复率；
- Top 3 高风险段：`pid`、风险值、主因、建议算子；
- 报告路径与工作台；
- 下一步选项：只降 AI 味（`aigc`）、只降重（`dedupe`）、双降（`both`）。

边界：风险分是篇内相对指标，不是任何平台的检测概率；不要承诺「能过知网」。
