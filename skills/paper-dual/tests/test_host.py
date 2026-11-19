# -*- coding: utf-8 -*-
"""宿主层回归测试：跑真实 CLI，不 mock 引擎。

覆盖：自检 / 诊断 / 任务 / 提示词 / 预检 / 回填 / 复检 / 回退 / 外部清单拦截 / 工作台。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
CLI = PLUGIN / "host" / "paperdual.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample.docx"

GOOD_EDIT = {
    "pid": "p0004",
    "op": "O2",
    "text": (
        "生成式人工智能技术这几年推进得很快，高校写作教学也跟着发生了变化。"
        "智能写作工具能给学生提供即时的语言帮助，表达效率确实提高了。"
        "可这类工具一普及，学术诚信方面又冒出新的问题，教育研究者需要认真看待。"
        "教学里怎么合理地引导技术使用，成了眼下要解决的事。"
        "本研究就从教师的角度出发，看看人工智能工具在写作教学中应当放在什么位置。"
    ),
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HostFlow(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="paper-dual-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        self.env = {**os.environ, "PAPER_DUAL_STATE_DIR": str(self.state), "PYTHONUTF8": "1"}
        self.work = Path(tempfile.mkdtemp(prefix="paper-dual-doc-"))
        self.addCleanup(lambda: shutil.rmtree(self.work, ignore_errors=True))
        self.docx = self.work / "论文.docx"
        shutil.copy2(FIXTURE, self.docx)

    def cli(self, *args, expect=0):
        result = subprocess.run(
            [sys.executable, str(CLI), *args, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            timeout=300,
        )
        payload = None
        raw = (result.stdout or "").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                self.fail(f"输出不是 JSON：{raw[:400]}\nstderr={result.stderr[:400]}")
        if expect is not None:
            self.assertEqual(result.returncode, expect, msg=f"{args}\n{raw[:800]}\n{result.stderr[:400]}")
        return payload

    def prepare(self):
        before = sha(self.docx)
        diagnosed = self.cli("diagnose", "--in", str(self.docx))
        self.assertEqual(sha(self.docx), before, "诊断不得修改原文件")
        self.assertEqual(diagnosed["summary"]["dist"]["high"], 3)
        planned = self.cli("plan", "--scope", "aigc")
        self.assertEqual(planned["versionId"], "v1")
        self.assertGreaterEqual(planned["tasks"], 1)
        return planned

    def test_doctor(self):
        payload = self.cli("doctor")
        self.assertTrue(payload["ok"])
        checks = {item["name"]: item["ok"] for item in payload["checks"]}
        self.assertTrue(checks["引擎完整"])
        self.assertTrue(checks["状态目录可写"])

    def test_full_flow(self):
        planned = self.prepare()
        prompts = self.cli("prompts", "--limit", "6")
        self.assertGreaterEqual(prompts["count"], 1)
        self.assertTrue(prompts["system"])
        self.assertTrue(all("system" not in row for row in prompts["rows"]))

        edits = Path(planned["editsPath"])
        edits.write_text(json.dumps(GOOD_EDIT, ensure_ascii=False) + "\n", encoding="utf-8")

        lint = self.cli("lint")
        self.assertEqual(lint["counts"]["would_reject"], 0)
        self.assertEqual(lint["counts"]["would_apply"], 1)

        applied = self.cli("apply")
        self.assertTrue(applied["verdict"]["passed"])
        self.assertFalse(applied["rolledBack"])
        self.assertTrue(applied["output"])
        self.assertFalse(applied["acceptance"]["aigcChecked"])
        self.assertLessEqual(applied["dup"]["after"], applied["dup"]["before"])

        cmp = self.cli("compare")
        self.assertEqual(cmp["count"], 1)
        self.assertEqual(cmp["rows"][0]["pid"], "p0004")

        workbench = self.cli("workbench")
        html = Path(workbench["path"]).read_text(encoding="utf-8")
        self.assertIn("论文双降", html)
        self.assertIn("p0004", html)

        export = self.work / "改稿.docx"
        self.cli("export", "--version", "v1", "--out", str(export))
        self.assertTrue(export.exists())

        status = self.cli("status")
        self.assertEqual(status["doc"]["activeVersionId"], "v1")

    def test_rejected_edit_rolls_back(self):
        planned = self.prepare()
        bad = {
            "pid": "p0004",
            "op": "O2",
            "text": "2024 年生成式人工智能技术进步很快，高校写作教学也变了。",
        }
        target = Path(planned["editsPath"])
        target.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")

        lint = self.cli("lint")
        self.assertEqual(lint["counts"]["would_reject"], 1)
        self.assertEqual(lint["rejected"][0]["reason"], "entity_added")

        applied = self.cli("apply", expect=1)
        self.assertTrue(applied["rolledBack"])
        self.assertFalse(applied["verdict"]["passed"])

    def test_foreign_pid_is_blocked(self):
        planned = self.prepare()
        target = Path(planned["editsPath"])
        target.write_text(
            json.dumps({"pid": "p9999", "op": "O2", "text": "随便改改"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        payload = self.cli("apply", expect=2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "E_FOREIGN_EDITS")
        self.assertIn("p9999", payload["unknown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
