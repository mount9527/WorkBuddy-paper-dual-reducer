# -*- coding: utf-8 -*-
"""插件结构自检：清单、技能、命令、子代理、模板。

    python -m unittest tests.test_manifest -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]


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


class Manifest(unittest.TestCase):
    def test_marketplace(self):
        path = REPO / ".codebuddy-plugin" / "marketplace.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "paper-dual-workbuddy")
        self.assertTrue(data["owner"]["name"])
        names = [p["name"] for p in data["plugins"]]
        self.assertIn("paper-dual", names)
        entry = next(p for p in data["plugins"] if p["name"] == "paper-dual")
        source = (REPO / entry["source"]).resolve()
        self.assertTrue(source.is_dir(), entry["source"])
        self.assertTrue((source / ".codebuddy-plugin" / "plugin.json").exists())

    def test_plugin_manifest(self):
        data = json.loads((PLUGIN / ".codebuddy-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "paper-dual")
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["license"], "MIT")
        self.assertTrue((PLUGIN / "skills" / "paper-dual" / "SKILL.md").exists())
        # 规范：commands/agents/skills/hooks 必须在插件根，不能塞进 .codebuddy-plugin
        allowed = {"plugin.json"}
        inside = {p.name for p in (PLUGIN / ".codebuddy-plugin").iterdir() if p.is_file()}
        self.assertTrue(inside <= allowed, inside)

    def test_skill(self):
        meta = frontmatter(PLUGIN / "skills" / "paper-dual" / "SKILL.md")
        self.assertEqual(meta.get("name"), "paper-dual")
        self.assertTrue(meta.get("description"))
        self.assertTrue(meta.get("description_zh"))
        body = (PLUGIN / "skills" / "paper-dual" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("paperdual.py", "edits.jsonl", "prompts.jsonl", "不新增", "回退"):
            self.assertIn(needle, body)

    def test_commands(self):
        files = sorted((PLUGIN / "commands").glob("*.md"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            meta = frontmatter(path)
            self.assertTrue(meta.get("description"), path.name)
            body = path.read_text(encoding="utf-8")
            self.assertIn("paperdual.py", body, path.name)

    def test_agent(self):
        meta = frontmatter(PLUGIN / "agents" / "paper-dual-rewriter.md")
        self.assertEqual(meta.get("name"), "paper-dual-rewriter")
        self.assertTrue(meta.get("description"))
        self.assertIn("Write", meta.get("tools", ""))

    def test_workbench_template(self):
        html = (PLUGIN / "host" / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("__PAPER_DUAL_STATE__", html)
        self.assertIn("paper-dual-state", html)
        self.assertIn("口径边界", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
