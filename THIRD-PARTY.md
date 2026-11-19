# 第三方组件与许可

本仓库自身代码为 MIT（见 `LICENSE`）。以下组件按原许可证随技能分发。

## 1. paper-dual-reduce 引擎

- 位置：`skills/paper-dual/engine/`
- 来源：https://github.com/mount9527/skill-paper-dual-reducer
- 许可：MIT（`skills/paper-dual/engine/LICENSE`）
- 版权：Copyright (c) 2026 paper-dual-reduce contributors
- 说明：负责 docx 解析、六维诊断、排期、提示词构建、回填、篇级校验与报告。
  本技能未修改其算法逻辑；入口仍是 `run.py`，子命令与产物保持一致。

## 2. AIGC 考官

- 位置：`skills/paper-dual/aigc/`
- 来源：https://github.com/mount9527/paper-dual-reducer（`aigc/` 目录）
- 许可：MIT（`skills/paper-dual/aigc/LICENSE`）
- 版权：Copyright (c) 2026 mount9527
- 说明：`detector.py`（模型打分）、`diag.py`（9 维痕迹，纯正则）、`segscore.py`（逐块打分）、
  `segdiag.py`（逐段痕迹）、`bands.py` / `blocking.py` / `calib.json`（档位、冻结块、校准）。

## 3. AIGC 检测模型

- 模型：`yuchuantian/AIGC_detector_zhv3`
- 来源：https://huggingface.co/yuchuantian/AIGC_detector_zhv3
- 许可：Apache-2.0；基座 `chinese-roberta-wwm-ext` 亦为 Apache-2.0
- **不随本仓库分发**：仓库里只有模型 id 字符串，权重由使用者首次运行时从 HuggingFace 自行下载
  （约 409MB `pytorch_model.bin`）。下载与缓存由 `transformers` / `huggingface_hub` 完成。

## 4. 运行时依赖（不打包）

| 依赖 | 版本 | 许可 | 何时需要 |
|---|---|---|---|
| Python | ≥ 3.10 | PSF-2.0 | 始终 |
| torch | 2.9.1 | BSD-3-Clause | 仅 AIGC 考官 |
| transformers | 5.23.0 | Apache-2.0 | 仅 AIGC 考官 |
| LibreOffice | 任意较新版本 | MPL-2.0 | 仅 `.doc` 转换（可选，Windows 可用 Word COM 替代） |

## 5. 字体与图标

工作台只用系统字体（Segoe UI / PingFang SC / Microsoft YaHei / Georgia / Consolas），
不内嵌字体文件；图标均为内联 CSS/SVG，无第三方图标库。
