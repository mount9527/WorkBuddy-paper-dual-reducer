# 流程 · 只做诊断

用户要的是「看看哪里有问题」，不是改稿。不要顺手 `plan`。

## 触发说法

「诊断一下」「哪里像 AI 写的」「重复率多少」「先出个报告」

## 步骤

1. 自检（每个会话一次即可）：

```bash
python "<skill>/host/paperdual.py" doctor --json
```

2. 诊断（只读原文件，不改它）：

```bash
python "<skill>/host/paperdual.py" diagnose --in "<论文绝对路径>" --json
```

用户没给路径就向他要绝对路径，不要猜、不要去磁盘上搜。

3. 生成工作台并打开：

```bash
python "<skill>/host/paperdual.py" workbench --json
```

把返回的 `path` 用 `present_files` 展示。

4. 用中文向用户汇报（不要贴 JSON）：

- 可改写 / 锁定段数，风险分布高/中/低，篇内重复率；
- Top 3 高风险段：`pid`、风险值、主因、建议算子；
- 报告路径与工作台；
- 下一步选项：只降 AI 味（`aigc`）、只降重（`dedupe`）、双降（`both`）。

## 边界

风险分是篇内相对指标，不是任何平台的检测概率；不要承诺「能过知网」。
