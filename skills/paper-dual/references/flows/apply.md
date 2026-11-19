# 流程 · 回填并复检

用户已经有 `edits.jsonl`（自己贴的、子代理写的、或上一轮留下的），要把它落到 docx。

## 触发说法

「把改稿贴回去」「回填」「复检一下」「看看能不能过闸门」

## 步骤

1. 用户给了清单路径时先确认文件存在，再：

```bash
python "<skill>/host/paperdual.py" apply --edits "<清单路径>" --json
```

没给路径就直接：

```bash
python "<skill>/host/paperdual.py" apply --json
```

（默认读当前版本产物目录里的 `edits.jsonl`。）

2. 用户在对话里直接贴了 JSONL 时，不要把它塞进 shell 参数；用 `Write` 落到
   `<editsPath>`（`prompts` 或 `status` 会给出），再跑 `apply --json`。

3. 逐项汇报：

- 预检：可通过 / 会被拒；被拒的 `pid` + `reason` + `hint`；
- 回填：提交 / 接受 / 拒绝；
- 结构复检：通过 / 未通过；
- AIGC 复检：通过 / 未通过 / **未执行（依赖未安装）**；
- 篇内重复率 before → after；
- `rolledBack` 为 true 时，说明回退到哪一版、哪一项没过。

4. 通过后生成工作台与导出：

```bash
python "<skill>/host/paperdual.py" workbench --json
python "<skill>/host/paperdual.py" export --version <当前版本> --out "<用户指定路径>" --json
```

用 `present_files` 打开工作台和 docx。

不要美化失败：`acceptance.aigcChecked = false` 就是没验，不能写成「AIGC 已通过」。
