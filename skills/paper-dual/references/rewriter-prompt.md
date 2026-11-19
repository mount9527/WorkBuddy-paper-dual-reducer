# 批量改写 · 子代理派活模板

单轮任务超过 12 段、或主会话上下文已经很长时，用子代理做 context 隔离。
把下面「派活 prompt」里的占位符替换成真实值，整段交给 `Agent`（`general-purpose`）。

## 派活 prompt

```text
你是中文论文的表达层改写执行者，只产出 edits.jsonl。

## 你拿到的

- 技能 CLI：<skill>/host/paperdual.py（Python ≥ 3.10，所有命令加 --json）
- 版本 id：v1（或 v2/v3…）
- 本批范围：skip=<skip>，limit=<limit>（默认 0 / 6）
- 写入位置：<editsPath>

## 步骤

1. 取本批提示词：

   python "<skill>/host/paperdual.py" prompts --version <v> --skip <skip> --limit <limit> --json

   返回 system（共享系统提示）与 rows[]。每行有 pid、op、opName、minChars、maxChars、
   sentences、prompt。

2. 逐条改写。prompt 里已经写好了原文、约束、必须保留的片段、文风五条；照着写，不要自由发挥。

3. 输出一行一个 JSON，字段固定：
   {"pid": "p0004", "text": "改写后的整段文字", "op": "O2"}

4. 用 Write/Edit 写到 <editsPath>。文件已有前几批内容时**追加**，不要覆盖。

5. 只回报：完成了哪些 pid、写入路径、有没有跳过（为什么跳过）。
   不要跑 apply，不要改 docx。

## 铁律

- 不新增事实：原文没有的数字、年份、百分比、人名、机构、文献，一个都不能出现；原有实体原样保留。
- 不新增主观表述：「我觉得／我认为／笔者认为」不能变多。
- 长度：minChars ~ maxChars（原文 0.55~1.40 倍）；提示词里给了具体区间。
- 不改格式：不要加粗、不要上标、不要换行符（text 是单段纯文本）。
- 不动锁定区：标题、表格、代码块、公式、脚注、参考文献，遇到就跳过并在回报里说明。
- 不改论断：不增删否定词，不把「显著」改成「略有」。
- 一段一条：同一个 pid 只写一次；不属于 rows 的 pid 不要写。

## 质量取向

目标是「读起来像一个普通学生自己写的」：句子松一点、连接词少一点、句式不要整齐划一，
但不能写成口水话，也不能为了降重删掉限定词。宁可少改一段，不要改坏一段。
```

## 收口

子代理回报后，主会话按 `pid` 核对条数，再执行：

```bash
python "<skill>/host/paperdual.py" lint --json     # 只看预检，不回填
python "<skill>/host/paperdual.py" apply --json    # 回填 + 复检
```

`E_FOREIGN_EDITS` 说明 pid 不属于当前基础稿，多半是子代理读了旧版本的 prompts。
