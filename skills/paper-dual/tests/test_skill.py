# -*- coding: utf-8 -*-
"""技能结构自检：SKILL.md 元数据、流程文档、资源目录、模板占位。

    python -m unittest tests.test_skill -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[1]


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    data = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


class SkillLayout(unittest.TestCase):
    def test_frontmatter(self):
        meta = frontmatter(SKILL / "SKILL.md")
        self.assertEqual(meta.get("name"), "paper-dual")
        self.assertEqual(meta.get("version"), "1.1.0")
        self.assertEqual(meta.get("license"), "MIT")
        self.assertTrue(meta.get("description"))
        self.assertTrue(meta.get("description_zh"))
        self.assertIn("降", meta.get("description_zh", ""))

    def test_body_keeps_hard_rules(self):
        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("paperdual.py", "edits.jsonl", "prompts.jsonl", "不新增", "回退", "references/flows/"):
            self.assertIn(needle, body)

    def test_no_command_placeholders(self):
        """技能没有 slash command，文档里不该再有 $ARGUMENTS。"""
        for path in [SKILL / "SKILL.md", *(SKILL / "references").rglob("*.md")]:
            self.assertNotIn("$ARGUMENTS", path.read_text(encoding="utf-8"), path.name)

    def test_flows(self):
        flows = sorted((SKILL / "references" / "flows").glob("*.md"))
        self.assertEqual(
            {p.stem for p in flows},
            {"diagnose", "rewrite", "apply", "workbench"},
        )
        for path in flows:
            body = path.read_text(encoding="utf-8")
            self.assertIn("paperdual.py", body, path.name)
            self.assertIn("<skill>", body, path.name)

    def test_references(self):
        for name in ("gates.md", "operators.md", "thresholds.md", "compliance.md", "rewriter-prompt.md"):
            self.assertTrue((SKILL / "references" / name).is_file(), name)

    def test_bundled_engine(self):
        for rel in ("host/paperdual.py", "host/workbench.html", "engine/run.py", "aigc/detector.py"):
            self.assertTrue((SKILL / rel).is_file(), rel)

    def test_workbench_template(self):
        html = (SKILL / "host" / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("__PAPER_DUAL_STATE__", html)
        self.assertIn("paper-dual-state", html)
        self.assertIn("口径边界", html)

    def test_rewriter_prompt(self):
        body = (SKILL / "references" / "rewriter-prompt.md").read_text(encoding="utf-8")
        for needle in ("editsPath", '"pid"', "不要跑 apply", "minChars"):
            self.assertIn(needle, body)

    def test_plugin_form_gone(self):
        """已改为技能分发：仓库里不应再有插件市场残留。"""
        self.assertFalse((REPO / ".codebuddy-plugin").exists())
        self.assertFalse((REPO / "plugins").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
