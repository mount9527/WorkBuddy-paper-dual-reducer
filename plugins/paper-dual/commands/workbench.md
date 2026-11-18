---
description: "重新生成论文双降工作台并打开"
argument-hint: "[文档路径，可省略]"
---

重新生成工作台：

```bash
python "<插件根>/host/paperdual.py" workbench --json
```

参数 `$ARGUMENTS` 是文档路径时加上 `--doc "$ARGUMENTS"`；省略就用最近一篇。

把返回的 `path` 用 `present_files` 打开，并用一两句话说明当前版本与能不能继续改
（`doc.canCreateVersion`）。

如果用户要在面板里点按钮操作（预检、回填、切版本），改用本地服务：

```bash
python "<插件根>/host/paperdual.py" serve --port 8760
```

把 `http://127.0.0.1:8760/` 交给用户；只监听本机，不要外传。
