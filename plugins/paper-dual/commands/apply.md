---
description: "回填编辑清单并复检，不通过就回退"
argument-hint: "[edits.jsonl 的路径，可省略]"
---

回填当前版本的编辑清单。

1. 如果用户给了清单路径（`$ARGUMENTS`），先确认文件存在，再：

```bash
python "<插件根>/host/paperdual.py" apply --edits "$ARGUMENTS" --json
```

没给路径就直接：

```bash
python "<插件根>/host/paperdual.py" apply --json
```

（默认读当前版本产物目录里的 `edits.jsonl`。）

2. 如果用户是在对话里直接贴了 JSONL，不要把它塞进 shell 参数；用 `Write` 落到
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
python "<插件根>/host/paperdual.py" workbench --json
python "<插件根>/host/paperdual.py" export --version <当前版本> --out "<用户指定路径>" --json
```

用 `present_files` 打开工作台和 docx。

不要美化失败：`acceptance.aigcChecked = false` 就是没验，不能写成「AIGC 已通过」。
