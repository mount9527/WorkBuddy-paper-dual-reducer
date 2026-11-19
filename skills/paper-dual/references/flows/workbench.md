# 流程 · 打开工作台

## 触发说法

「打开工作台」「看面板」「看对照」「我要点按钮预检」

## 静态版（默认）

```bash
python "<skill>/host/paperdual.py" workbench --json
```

把返回的 `path` 用 `present_files` 打开，并用一两句话说明当前版本与能不能继续改
（`doc.canCreateVersion`）。

用户给了文档路径时加 `--doc "<路径>"`；省略就用最近一篇。

## 可交互版

用户要在面板里点按钮（预检、回填、切版本）时改用本地服务：

```bash
python "<skill>/host/paperdual.py" serve --port 8760
```

把 `http://127.0.0.1:8760/` 交给用户；只监听本机，不要外传。
进程随会话结束可能退出，退出就重开。
