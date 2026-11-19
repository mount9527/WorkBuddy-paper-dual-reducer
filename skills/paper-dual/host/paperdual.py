#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper-dual · WorkBuddy 宿主层

这里不是 DSH 插件的搬运，而是把「宿主」重新写了一遍（现作为技能内置 CLI 分发）：
DSH 侧的 Cordis 路由、ctx.tools 注册、左侧栏面板都不存在了，换成

    CLI（agent 调用，JSON 进 JSON 出）
      + 版本链状态机（原文 / 改稿 / 二改…五改）
      + 静态工作台 HTML（present_files 直接预览）
      + 可选的本地服务（127.0.0.1，给工作台提供写操作）

引擎（engine/run.py）与 AIGC 考官（aigc/*.py）是 MIT 授权的独立组件，
按原样内置；本文件只负责把它们编排成 WorkBuddy 能用的形状。

状态目录：~/.workbuddy/paper-dual（PAPER_DUAL_STATE_DIR 可重定向）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PLUGIN_VERSION = "1.1.0"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ENGINE = PLUGIN_ROOT / "engine"
AIGC = PLUGIN_ROOT / "aigc"
HOST = PLUGIN_ROOT / "host"
TEMPLATE = HOST / "workbench.html"

MAX_REVISIONS = 5
VERSION_RE = re.compile(r"^v([1-5])$")

LABELS = {0: "原文", 1: "改稿", 2: "二改", 3: "三改", 4: "四改", 5: "五改"}

CONSTRAINTS = [
    "不新增数字、年份、百分比、引用、书名与英文缩写",
    "不新增「我觉得／我认为」这类主观表述",
    "字数控制在原文的 0.55~1.40 倍",
    "标题、表格、代码块、公式、参考文献一律不动",
    "不改变论断方向与结论强度",
]

DIAG_DIMENSIONS = [
    ("template", "模板套话"),
    ("passive", "被动句"),
    ("burstiness", "句长起伏"),
    ("symmetry", "结构对称"),
    ("nested", "层层嵌套"),
    ("colon", "冒号列举"),
    ("punct", "标点节奏"),
    ("colloquial", "口语化"),
    ("dash", "破折号"),
]

BAND_LABELS = {
    "safe": "AI 特征不明显",
    "warn": "存在 AI 特征段落",
    "risk": "AI 特征显著",
}

AIGC_MODEL = "yuchuantian/AIGC_detector_zhv3"


# ------------------------------------------------------------------ 基础工具


class UserError(Exception):
    """可预期的失败：参数、状态、环境。CLI 统一转成 {ok:false,error}。"""

    def __init__(self, message: str, code: str = "E_USER", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clip(text, limit: int = 400) -> str:
    text = "" if text is None else str(text)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sha256_file(path) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def read_text(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return default


def write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def state_root() -> Path:
    raw = os.environ.get("PAPER_DUAL_STATE_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".workbuddy" / "paper-dual"
    return base.resolve()


def state_path() -> Path:
    return state_root() / "state.json"


def config_path() -> Path:
    return state_root() / "config.json"


def load_state() -> dict:
    data = read_json(state_path(), {}) or {}
    if not isinstance(data.get("documents"), dict):
        data["documents"] = {}
    data.setdefault("version", 1)
    data.setdefault("activeDocKey", None)
    return data


def save_state(state: dict) -> dict:
    write_json(state_path(), state)
    return state


def load_config() -> dict:
    data = read_json(config_path(), {}) or {}
    return {
        "pythonPath": str(data.get("pythonPath") or "").strip(),
        "hfCacheDir": str(data.get("hfCacheDir") or "").strip(),
        "aiLabel": data.get("aiLabel") if data.get("aiLabel") in (0, 1) else None,
    }


def save_config(patch: dict) -> dict:
    config = load_config()
    config.update({k: v for k, v in patch.items() if v is not None})
    write_json(config_path(), config)
    return config


def slugify(stem: str, limit: int = 32) -> str:
    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", stem, flags=re.UNICODE)
    stem = re.sub(r"-{2,}", "-", stem).strip("-")
    return (stem or "paper")[:limit]


def doc_key(path) -> str:
    return sha1_text(str(path).lower())[:10]


def normalize_path(raw: str) -> Path:
    text = str(raw or "").strip()
    text = text.strip("\"'“”‘’")
    if text.lower().startswith("file:"):
        from urllib.request import url2pathname

        text = url2pathname(urlparse(text).path)
    return Path(text).expanduser().resolve()


def run_proc(cmd, cwd=None, timeout: int = 180, env_extra=None) -> dict:
    env = {**os.environ, **(env_extra or {})}
    started = time.time()
    try:
        proc = subprocess.run(
            [str(x) for x in cmd],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return {
            "code": proc.returncode,
            "out": proc.stdout or "",
            "err": proc.stderr or "",
            "seconds": round(time.time() - started, 2),
        }
    except FileNotFoundError as exc:
        return {"code": 127, "out": "", "err": str(exc), "seconds": 0}
    except subprocess.TimeoutExpired as exc:
        return {
            "code": 124,
            "out": (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "err": f"超时（{timeout}s）",
            "seconds": round(time.time() - started, 2),
        }


def engine_run(args, timeout: int = 300) -> dict:
    result = run_proc(
        [sys.executable, str(ENGINE / "run.py"), *args],
        cwd=ENGINE,
        timeout=timeout,
        env_extra={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return result


def engine_script(script: str, args, timeout: int = 180) -> dict:
    return run_proc(
        [sys.executable, str(ENGINE / script), *args],
        cwd=ENGINE,
        timeout=timeout,
        env_extra={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )


def engine_check() -> None:
    if not (ENGINE / "run.py").exists():
        raise UserError(f"技能目录里缺少引擎：{ENGINE / 'run.py'}（技能安装不完整，请重新安装）")


def aigc_python() -> str:
    configured = load_config().get("pythonPath")
    return configured or sys.executable


def script_run(script, args, timeout: int = 300, python: str | None = None) -> dict:
    return run_proc(
        [python or sys.executable, str(AIGC / script), *args],
        cwd=AIGC,
        timeout=timeout,
        env_extra={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )


# ------------------------------------------------------------------ 文档入口


def convert_doc(src: Path) -> tuple[Path, str]:
    """把 .doc 转成 .docx 副本；原文件一个字节都不动。"""
    target_dir = state_root() / "converted"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{doc_key(src)}-{slugify(src.stem)}.docx"
    if target.exists() and target.stat().st_mtime >= src.stat().st_mtime:
        return target, "cache"

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        result = run_proc(
            [soffice, "--headless", "--convert-to", "docx", "--outdir", str(target_dir), str(src)],
            timeout=180,
        )
        produced = target_dir / f"{src.stem}.docx"
        if result["code"] == 0 and produced.exists():
            os.replace(produced, target)
            return target, "libreoffice"

    if os.name == "nt":
        src_ps = str(src).replace("'", "''")
        dst_ps = str(target).replace("'", "''")
        word_script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$src = '{src_ps}'",
                f"$dst = '{dst_ps}'",
                "$word = New-Object -ComObject Word.Application",
                "$word.Visible = $false",
                "$word.DisplayAlerts = 0",
                "try {",
                "  $doc = $word.Documents.Open($src, $false, $true)",
                "  $doc.SaveAs2($dst, 16)",
                "  $doc.Close($false)",
                "} finally {",
                "  $word.Quit()",
                "}",
            ]
        )
        encoded = base64.b64encode(word_script.encode("utf-16-le")).decode("ascii")
        result = run_proc(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            timeout=180,
        )
        if result["code"] == 0 and target.exists():
            return target, "word-com"

    raise UserError(
        "这个 .doc 转不了 .docx：本机没有 LibreOffice，也没能调用 Word。"
        "请先在 Word 里「另存为 .docx」再诊断（原文件不会被改动）。",
        code="E_CONVERT",
    )


def ensure_docx(raw: str) -> dict:
    path = normalize_path(raw)
    if not path.exists():
        raise UserError(f"找不到文件：{path}")
    if path.is_dir():
        raise UserError(f"这是一个目录，不是论文文件：{path}")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        if path.stat().st_size < 1024:
            raise UserError(f"文件太小，不像一个正常的 docx：{path}")
        return {"input": path, "original": path, "converted": False, "method": "none"}
    if suffix == ".doc":
        converted, method = convert_doc(path)
        return {"input": converted, "original": path, "converted": True, "method": method}
    raise UserError(
        f"只支持 .doc / .docx，收到的是 {suffix or '无扩展名'}：{path.name}。"
        "PDF、扫描件、图片请先转成 Word。",
        code="E_FORMAT",
    )


def summarize(doc: dict, profile: dict) -> dict:
    by_pid = {p.get("pid"): p for p in doc.get("paragraphs", [])}
    top = []
    for item in (profile.get("top") or [])[:12]:
        pid = item.get("pid")
        causes = item.get("causes") or []
        top.append(
            {
                "pid": pid,
                "risk": item.get("risk"),
                "level": item.get("level"),
                "cause": (causes[0].get("name") if causes else "—"),
                "op": item.get("suggest_op") or "—",
                "chars": item.get("chars"),
                "dupRatio": item.get("dup_ratio") or 0,
                "text": clip((by_pid.get(pid) or {}).get("text", ""), 200),
            }
        )
    dup = ((profile.get("duplicates") or {}).get("stats") or {})
    return {
        "stats": doc.get("stats") or {},
        "dist": profile.get("summary") or {},
        "repeatRate": dup.get("repeat_rate") or 0,
        "lockReasons": doc.get("lock_reasons") or {},
        "paragraphs": len(profile.get("paragraphs") or []),
        "top": top,
    }


# ------------------------------------------------------------------ 状态与版本


def doc_dir(doc: dict) -> Path:
    return state_root() / "docs" / doc["dir"]


def archive_doc(doc: dict, reason: str) -> str | None:
    src = doc_dir(doc)
    if not src.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = state_root() / "archive" / f"{doc['dir']}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    meta = {"reason": reason, "archivedAt": now_iso(), "from": str(src)}
    write_json(target / "archived.json", meta)
    return str(target)


def get_doc(key: str) -> dict | None:
    return load_state()["documents"].get(key)


def require_doc(raw: str | None = None, rediagnose: bool = False) -> dict:
    state = load_state()
    key = None
    if raw:
        key = doc_key(normalize_path(raw))
        if key not in state["documents"]:
            return diagnose(raw)
    else:
        key = state.get("activeDocKey")
        if not key or key not in state["documents"]:
            raise UserError("还没有诊断过任何论文。先跑：paperdual.py diagnose --in <论文.docx>")
    doc = state["documents"][key]
    if rediagnose or doc_changed(doc):
        if rediagnose:
            return diagnose(doc["docPath"], force=True)
        raise UserError(
            "原文件在诊断后被修改了。请先重新诊断（--rediagnose），再用新的产物继续。",
            code="E_STALE",
        )
    return doc


def doc_changed(doc: dict) -> bool:
    stored = doc.get("sourceSha") or ""
    if not stored:
        return False
    path = Path(doc.get("docPath") or "")
    if not path.exists():
        return True
    return sha256_file(path) != stored


def version_by_id(doc: dict, vid: str | None) -> dict:
    versions = doc.get("versions") or {}
    if not vid:
        vid = doc.get("activeVersionId") or "original"
    if vid not in versions:
        raise UserError(f"版本不存在：{vid}（可用：{'、'.join(versions.keys())}）")
    return versions[vid]


def resolve_version(doc: dict, target: str | None) -> dict:
    text = str(target or "").strip()
    if text in ("", "active"):
        return version_by_id(doc, doc.get("activeVersionId"))
    if text in ("original", "原文"):
        return version_by_id(doc, "original")
    if text in ("rewritten", "latest", "改稿"):
        latest = latest_downloadable(doc)
        if latest:
            return latest
        return version_by_id(doc, "original")
    if text.isdigit():
        text = f"v{text}"
    return version_by_id(doc, text)


def latest_downloadable(doc: dict) -> dict | None:
    candidates = [
        v for v in (doc.get("versions") or {}).values()
        if v["id"] != "original" and v.get("docx") and Path(v["docx"]).exists()
    ]
    candidates.sort(key=lambda v: v.get("index", 0))
    return candidates[-1] if candidates else None


def public_version(doc: dict, version: dict) -> dict:
    docx = version.get("docx")
    return {
        "id": version["id"],
        "index": version.get("index", 0),
        "label": version.get("label") or version["id"],
        "status": version.get("status") or "unknown",
        "baseId": version.get("baseId"),
        "baseLabel": version.get("baseLabel"),
        "passed": version.get("passed"),
        "downloadable": bool(docx and Path(docx).exists()),
        "docx": docx if docx and Path(docx).exists() else None,
        "createdAt": version.get("createdAt"),
        "updatedAt": version.get("updatedAt"),
        "scope": version.get("scope"),
        "tasks": version.get("tasks"),
        "prompts": version.get("prompts"),
        "verifiedAigc": version.get("verifiedAigc"),
        "rolledBackTo": version.get("rolledBackTo"),
        "error": version.get("error"),
    }


def save_doc(doc: dict) -> dict:
    state = load_state()
    doc["updatedAt"] = now_iso()
    state["documents"][doc["key"]] = doc
    state["activeDocKey"] = doc["key"]
    save_state(state)
    return doc


def set_active(doc: dict, vid: str) -> dict:
    doc["activeVersionId"] = vid
    return save_doc(doc)


def last_accepted(doc: dict, start: dict) -> dict:
    cursor = start
    seen = set()
    while cursor and cursor["id"] not in seen:
        seen.add(cursor["id"])
        if cursor["id"] == "original" or cursor.get("passed") is True:
            return cursor
        cursor = (doc.get("versions") or {}).get(cursor.get("baseId") or "")
    return doc["versions"]["original"]


def doc_json_path(doc: dict, version: dict) -> Path | None:
    raw = str(version.get("docJson") or "").strip()
    if raw:
        path = Path(raw)
        if path.is_file():
            return path
    fallback = Path(version["workdir"]) / ("doc.json" if version["id"] == "original" else "base-doc.json")
    return fallback if fallback.is_file() else None


def version_chars(version: dict) -> int | None:
    """这一版当前正文的字数：回填过的看 check-current，没回填的看该版基础 doc.json。"""
    workdir = Path(version["workdir"])
    for path in (workdir / "check-current" / "doc.json", workdir / "doc.json"):
        if path.is_file():
            stats = (read_json(path, {}) or {}).get("stats") or {}
            if stats.get("chars") is not None:
                return int(stats["chars"])
    return None


def ensure_doc_json(doc: dict, version: dict) -> Path:
    existing = doc_json_path(doc, version)
    if existing:
        return existing
    if version["id"] == "original":
        raise UserError("缺 doc.json：请重新诊断这篇论文。")
    source = version.get("baseDocx") or version.get("docx")
    if not source or not Path(source).exists():
        raise UserError(f"{version['label']}还没有可解析的 docx")
    out = Path(version["workdir"]) / "base-doc.json"
    result = engine_script("scripts/inspect_docx.py", ["--in", source, "--out", str(out)], timeout=180)
    if result["code"] != 0 or not out.exists():
        raise UserError(f"解析 {version['label']} 失败：{clip(result['err'] or result['out'])}")
    version["docJson"] = str(out)
    return out


# ------------------------------------------------------------------ 诊断


def diagnose(raw: str, force: bool = False) -> dict:
    engine_check()
    conv = ensure_docx(raw)
    source = conv["original"]
    key = doc_key(source)
    state = load_state()
    previous = state["documents"].get(key)
    source_sha = sha256_file(source)

    if previous and not force and previous.get("sourceSha") == source_sha:
        if (Path(previous["workdir"]) / "doc.json").exists():
            set_active(previous, previous.get("activeVersionId") or "original")
            return previous

    archived = None
    if previous:
        archived = archive_doc(previous, "原文件内容变化，重新诊断")
        state = load_state()
        state["documents"].pop(key, None)
        save_state(state)

    root = state_root() / "docs" / f"{slugify(source.stem)}-{key}"
    if root.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = state_root() / "archive" / f"{root.name}-{stamp}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(target))
        archived = str(target)
    root.mkdir(parents=True, exist_ok=True)
    original_dir = root / "original"
    original_dir.mkdir(parents=True, exist_ok=True)

    result = engine_run(
        ["diagnose", "--in", str(conv["input"]), "--outdir", str(original_dir)],
        timeout=300,
    )
    doc_json = original_dir / "doc.json"
    profile_json = original_dir / "profile.json"
    if result["code"] != 0 or not doc_json.exists() or not profile_json.exists():
        shutil.rmtree(root, ignore_errors=True)
        hint = ""
        if re.search(r"No such file|can't open file|不是内部或外部命令", result["err"] + result["out"], re.I):
            hint = "（python 不在 PATH，或技能目录被移动过）"
        raise UserError(f"引擎诊断失败：{clip(result['err'] or result['out'], 700)}{hint}")

    snapshot = original_dir / "source.docx"
    shutil.copy2(conv["input"], snapshot)

    doc = {
        "key": key,
        "dir": root.name,
        "docPath": str(source),
        "engineInput": str(conv["input"]),
        "title": source.name,
        "sourceSha": source_sha,
        "engineSha": sha256_file(conv["input"]),
        "converted": conv["converted"],
        "convertMethod": conv["method"],
        "workdir": str(root),
        "outdir": str(original_dir),
        "createdAt": previous.get("createdAt") if previous else now_iso(),
        "updatedAt": now_iso(),
        "activeVersionId": "original",
        "archivedPrevious": archived,
        "versions": {
            "original": {
                "id": "original",
                "index": 0,
                "label": LABELS[0],
                "status": "diagnosed",
                "baseId": None,
                "baseLabel": None,
                "workdir": str(original_dir),
                "docJson": str(doc_json),
                "docx": str(snapshot),
                "baseDocx": None,
                "scope": None,
                "passed": None,
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
            }
        },
    }
    doc["summary"] = summarize(read_json(doc_json, {}) or {}, read_json(profile_json, {}) or {})
    save_doc(doc)
    return doc


def doc_summary(doc: dict) -> dict:
    root = Path(doc["workdir"])
    doc_json = root / "original" / "doc.json"
    profile = root / "original" / "profile.json"
    if doc_json.exists() and profile.exists():
        doc["summary"] = summarize(read_json(doc_json, {}) or {}, read_json(profile, {}) or {})
    return doc


# ------------------------------------------------------------------ 9 维与逐段定位


def texts_of(doc_json: Path) -> dict:
    data = read_json(doc_json, {}) or {}
    paragraphs = data.get("paragraphs") or []

    def join(rows):
        return "\n\n".join(str(p.get("text") or "").strip() for p in rows if str(p.get("text") or "").strip())

    return {
        "all": join(paragraphs),
        "rewritable": join([p for p in paragraphs if not p.get("locked")]),
        "paragraphs": len(paragraphs),
    }


def diag_text(text: str, workdir: Path, tag: str) -> dict:
    if not text.strip():
        raise UserError("没有可诊断的正文文本")
    folder = workdir / "aigc"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"diag-{tag}.txt"
    write_text(target, text)
    result = script_run("diag.py", ["--file", str(target), "--json"], timeout=120)
    raw = (result["out"] or "").strip()
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
    if payload is None:
        raise UserError(f"9 维诊断失败：{clip(result['err'] or raw, 500)}")
    return payload


def diag_version(doc: dict, version: dict) -> dict:
    """对某个版本文本做 9 维痕迹诊断；结果缓存在版本目录。"""
    workdir = Path(version["workdir"])
    cache = workdir / "aigc" / "diag.json"
    cached = read_json(cache)
    if cached and cached.get("docSha") == version.get("docxSha"):
        return cached

    doc_json = ensure_doc_json(doc, version)
    texts = texts_of(doc_json)
    result = diag_text(texts["all"], workdir, version["id"])
    gate_all = bool(result.get("style_gate_passed"))
    gate_rewritable = gate_all
    if texts["rewritable"] and texts["rewritable"] != texts["all"]:
        rw = diag_text(texts["rewritable"], workdir, f"{version['id']}-rw")
        gate_rewritable = bool(rw.get("style_gate_passed"))
    payload = {
        "at": now_iso(),
        "versionId": version["id"],
        "docSha": version.get("docxSha"),
        "result": result,
        "gateAll": gate_all,
        "gateRewritable": gate_rewritable,
    }
    write_json(cache, payload)
    return payload


def active_risks(result: dict) -> set:
    names = set()
    for key, label in DIAG_DIMENSIONS:
        item = (result or {}).get(key) or {}
        if item.get("risk") or item.get("gate_failed") or (key == "burstiness" and item.get("level") == "low"):
            names.add(label)
    return names


def build_hints(result: dict) -> list:
    data = result or {}
    out: list[str] = []

    def push(text):
        if text and text not in out:
            out.append(text)

    for item in data.get("todo") or []:
        push(item)
    if (data.get("template") or {}).get("risk"):
        push("删掉「随着…」「在…方面」「综上所述」这类套话框架，改成具体主语的陈述")
    if (data.get("passive") or {}).get("risk"):
        push("把被动句改成主动句，补出施事者")
    if (data.get("burstiness") or {}).get("level") == "low":
        push("打破句长均齐：短句与长句交替，避免连续三句长度接近")
    if (data.get("symmetry") or {}).get("risk"):
        push("打破段落对称：相邻段落句数不要雷同")
    if (data.get("nested") or {}).get("risk"):
        push("拆开层层嵌套的从句，一句只说一件事")
    if (data.get("colon") or {}).get("risk"):
        push("减少冒号并列与编号罗列，改成叙述式表达")
    if (data.get("punct") or {}).get("risk"):
        push("标点节奏：不要用句号把句子切成碎片，也不要一句超过 60 字")
    if (data.get("dash") or {}).get("gate_failed"):
        push("减少破折号，每段最多 1 个")
    if (data.get("colloquial") or {}).get("gate_failed"):
        push("删除口语词，保持书面语（语体门禁会把口语词判为不合格）")
    return out


def segdiag(doc_json: Path, out: Path) -> list:
    result = script_run("segdiag.py", ["--doc", str(doc_json), "--out", str(out)], timeout=120)
    payload = read_json(out, {}) or {}
    if result["code"] != 0 and not payload:
        return []
    return payload.get("paragraphs") or []


# ------------------------------------------------------------------ AIGC 考官


_detector_cache: dict = {"key": None, "value": None}


def detector_state(force: bool = False) -> dict:
    python = aigc_python()
    cache_key = f"{python}|{os.environ.get('HF_HOME', '')}|{load_config().get('hfCacheDir', '')}"
    if not force and _detector_cache["key"] == cache_key and _detector_cache["value"]:
        return _detector_cache["value"]
    probe = "\n".join(
        [
            "import json",
            'out={"ready":True,"versions":{}}',
            'for name in ("torch","transformers"):',
            "    try:",
            "        m=__import__(name)",
            '        out["versions"][name]=str(getattr(m,"__version__","unknown"))',
            "    except Exception as e:",
            '        out["ready"]=False',
            '        out["versions"][name]=None',
            '        out["error"]=type(e).__name__+": "+str(e)',
            "print(json.dumps(out,ensure_ascii=False))",
        ]
    )
    result = run_proc(
        [python, "-c", probe],
        cwd=AIGC,
        timeout=90,
        env_extra={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    line = (result["out"] or "").strip().splitlines()
    payload = None
    if line:
        try:
            payload = json.loads(line[-1])
        except ValueError:
            payload = None
    if payload is None:
        payload = {
            "ready": False,
            "versions": {"torch": None, "transformers": None},
            "error": clip(result["err"] or result["out"] or "无法探测 Python 依赖", 400),
        }
    payload["python"] = python
    _detector_cache.update({"key": cache_key, "value": payload})
    return payload


def model_cache_root() -> Path:
    configured = load_config().get("hfCacheDir")
    if configured:
        return Path(configured).expanduser()
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"])
    return state_root() / "hf-cache"


def aigc_setup(det: dict | None = None) -> dict:
    det = det or {}
    venv = os.environ.get("PAPER_DUAL_AIGC_HOME", str(Path.home() / "paper-dual-aigc"))
    cache = load_config().get("hfCacheDir") or str(Path(venv) / "hf-cache")
    python = load_config().get("pythonPath") or str(Path(venv) / "Scripts" / "python.exe")
    # 本机没有 py 启动器时（装的是独立 Python 或应用自带解释器），退回当前解释器建 venv
    creator = "py -3.12 -m venv" if shutil.which("py") else f'& "{sys.executable}" -m venv'
    install = "\n".join(
        [
            f'$Venv = "{venv}"',
            f'$Cache = "{cache}"',
            '$env:HF_ENDPOINT = "https://hf-mirror.com"',
            '$env:HF_HUB_DISABLE_XET = "1"',
            "New-Item -ItemType Directory -Force -Path $Venv,$Cache | Out-Null",
            f"{creator} $Venv",
            '& "$Venv\\Scripts\\python.exe" -m pip install --upgrade pip --index-url https://pypi.mirrors.ustc.edu.cn/simple/',
            '& "$Venv\\Scripts\\python.exe" -m pip install torch==2.9.1 transformers==5.23.0 --index-url https://pypi.mirrors.ustc.edu.cn/simple/',
            '& "$Venv\\Scripts\\python.exe" -c "import torch, transformers; print(torch.__version__, transformers.__version__)"',
        ]
    )
    return {
        "required": ["torch==2.9.1", "transformers==5.23.0"],
        "missing": [k for k, v in (det.get("versions") or {}).items() if not v],
        "versions": det.get("versions") or {},
        "venv": venv,
        "python": python,
        "modelCacheDir": cache,
        "installScript": install,
        "manualSteps": [
            f"1. PowerShell 粘贴安装命令（venv：{venv}）",
            f"2. 配置解释器：paperdual.py config --python \"{python}\"",
            f"3. 配置模型缓存：paperdual.py config --hf-cache \"{cache}\"",
            "4. 跑 paperdual.py aigc --calibrate；首次会下载约 409MB 权重。",
        ],
        "model": AIGC_MODEL,
        "modelUrl": f"https://huggingface.co/{AIGC_MODEL}",
        "note": "AIGC 考官需要 torch + transformers；未安装时不会生成或显示任何分数。",
    }


def aigc_status() -> dict:
    det = detector_state()
    calib = read_json(AIGC / "calib.json", {}) or {}
    return {
        "ok": True,
        "detectorReady": bool(det.get("ready")),
        "mode": "model" if det.get("ready") else "unavailable",
        "model": AIGC_MODEL,
        "python": det.get("python") or aigc_python(),
        "diagReady": (AIGC / "diag.py").exists(),
        "versions": det.get("versions") or {},
        "calibration": {
            "version": calib.get("version"),
            "calibrated": calib.get("calibrated") is True,
            "thrHi": calib.get("thr_hi"),
            "thrHard": calib.get("thr_hard"),
            "safeMax": (calib.get("band") or {}).get("safe_max"),
            "warnMax": (calib.get("band") or {}).get("warn_max"),
        },
        "setup": aigc_setup(det),
    }


def share_of(result: dict):
    value = result.get("ai_share")
    if isinstance(value, (int, float)):
        return float(value)
    legacy = result.get("ai_prob")
    return float(legacy) if isinstance(legacy, (int, float)) else None


def measurement_of(result: dict) -> str:
    return "ai_share" if isinstance(result.get("ai_share"), (int, float)) else "legacy_mean"


def ensure_block_plan(doc: dict, base: dict) -> Path:
    plan = Path(base["workdir"]) / "aigc" / "block-plan.json"
    if plan.exists():
        return plan
    doc_json = ensure_doc_json(doc, base)
    folder = Path(base["workdir"]) / "aigc"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "segscore-base.json"
    args = ["--doc", str(doc_json), "--out", str(out), "--plan-out", str(plan)]
    label = load_config().get("aiLabel")
    if label in (0, 1):
        args += ["--ai-label", str(label)]
    result = script_run("segscore.py", args, timeout=900, python=aigc_python())
    if result["code"] != 0 or not plan.exists():
        raise UserError(f"冻结块计划生成失败：{clip(result['err'] or result['out'], 600)}", code="E_AIGC")
    write_json(folder / "segscore-base.result.json", read_json(out, {}) or {})
    return plan


def aigc_score(doc: dict, version: dict) -> dict:
    det = detector_state()
    if not det.get("ready"):
        raise UserError(
            "AIGC 真机依赖未安装：先装 torch + transformers，再用 config --python 指向那个 venv。"
            "未安装时不会生成或显示分数。",
            code="AIGC_DEPS_MISSING",
            setup=aigc_setup(det),
        )
    if version["id"] == "original":
        base = version
    else:
        base = version_by_id(doc, version.get("baseId") or "original")
    plan = ensure_block_plan(doc, base)
    doc_json = ensure_doc_json(doc, version)
    folder = Path(version["workdir"]) / "aigc"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "segscore.json"
    args = ["--doc", str(doc_json), "--out", str(out), "--plan", str(plan)]
    label = load_config().get("aiLabel")
    if label in (0, 1):
        args += ["--ai-label", str(label)]
    result = script_run("segscore.py", args, timeout=1200, python=aigc_python())
    payload = read_json(out, {}) or {}
    if result["code"] != 0 or not payload:
        raise UserError(f"逐段打分失败：{clip(result['err'] or result['out'], 700)}", code="E_AIGC")
    write_json(folder / "score.json", {
        "at": now_iso(),
        "versionId": version["id"],
        "plan": str(plan),
        "result": payload,
    })
    if isinstance(payload.get("ai_index"), int) and payload["ai_index"] >= 0:
        config = load_config()
        if config.get("aiLabel") is None:
            save_config({"aiLabel": payload["ai_index"]})
    return payload


def aigc_missing_payload(det: dict) -> dict:
    return {
        "ok": False,
        "code": "AIGC_DEPS_MISSING",
        "error": "AIGC 真机依赖未安装：torch + transformers 缺失。本技能不会编一个分数出来。",
        "setup": aigc_setup(det),
    }


def aigc_calibrate() -> dict:
    det = detector_state(force=True)
    if not det.get("ready"):
        return aigc_missing_payload(det)
    result = script_run("detector.py", ["--calibrate"], timeout=1800, python=aigc_python())
    raw = (result["out"] or "").strip()
    payload = None
    if raw:
        try:
            payload = json.loads(raw.splitlines()[-1])
        except ValueError:
            payload = None
    if result["code"] != 0 or payload is None:
        return {"ok": False, "error": clip(result["err"] or raw, 700)}
    if payload.get("ai_label") in (0, 1):
        save_config({"aiLabel": payload["ai_label"]})
    return {"ok": True, "calibration": payload}


# ------------------------------------------------------------------ 任务生成


def create_revision(doc: dict, base: dict, scope: str) -> dict:
    index = int(base.get("index") or 0) + 1
    if index > MAX_REVISIONS:
        raise UserError(f"最多支持五改，{base['label']}已经是最后一版。", code="E_LIMIT")
    vid = f"v{index}"
    if vid in doc["versions"]:
        raise UserError(f"{LABELS[index]}（{vid}）已经生成过，请先切换版本或重新开始。", code="E_EXISTS")
    workdir = Path(doc["workdir"]) / vid
    workdir.mkdir(parents=True, exist_ok=True)
    base_docx = Path(base.get("docx") or "")
    if not base_docx.exists():
        raise UserError(f"{base['label']}还没有 docx，不能作为下一轮基础。", code="E_BASE")
    shutil.copy2(base_docx, workdir / "base.docx")
    version = {
        "id": vid,
        "index": index,
        "label": LABELS[index],
        "status": "planned",
        "baseId": base["id"],
        "baseLabel": base["label"],
        "workdir": str(workdir),
        "baseDocx": str(workdir / "base.docx"),
        "docx": None,
        "docJson": None,
        "scope": scope,
        "passed": None,
        "tasks": 0,
        "prompts": 0,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    doc["versions"][vid] = version
    return version


def build_instruction(version: dict, scope: str) -> str:
    workdir = Path(version["workdir"])
    lines = [
        f"当前基础：{version['baseLabel']}（{version['baseId']}）；本轮将保存为：{version['label']}（{version['id']}）。",
        "",
        "请按下面的提示词逐段改写，产出编辑清单（一行一条 JSON）：",
        f"  提示词：{workdir / 'prompts.jsonl'}",
        f"  编辑清单：{workdir / 'edits.jsonl'}",
        "",
        '每行格式：{"pid": "p0004", "text": "改写后的整段文字", "op": "O2"}',
        "",
        "约束：",
    ]
    for i, item in enumerate(CONSTRAINTS, 1):
        lines.append(f"{i}. {item}")
    if scope in ("aigc", "both"):
        lines.append("6. AIGC 侧：优先改高贡献块，不要为了「像人写的」而写口水话")
    if scope == "both":
        lines.append("7. 重复侧：同一内容只讲一遍，可以合并、回指或概括；不得删掉限定词")
    lines += [
        "",
        "写完后跑：python host/paperdual.py apply --json",
        "（预检 → 回填 → 复检；复检不过会保留本版但不切为当前稿）",
    ]
    return "\n".join(lines)


def plan_document(
    raw: str | None,
    base_id: str | None,
    scope: str,
    max_tasks: int | None,
    focus_pids: list[str] | None,
    use_detector: bool,
    rediagnose: bool,
) -> dict:
    engine_check()
    doc = require_doc(raw, rediagnose=rediagnose)
    if doc_changed(doc):
        raise UserError("原文件在诊断后被修改了，先重新诊断。", code="E_STALE")
    base = version_by_id(doc, base_id or doc.get("activeVersionId"))
    if base["id"] != "original" and not (base.get("docx") and Path(base["docx"]).exists()):
        raise UserError(f"{base['label']}还没有回填出 docx，不能作为下一轮基础。", code="E_BASE")

    index = int(base.get("index") or 0) + 1
    if index > MAX_REVISIONS:
        raise UserError(f"最多支持五改，{base['label']}已经是最后一版。", code="E_LIMIT")

    caps = {"both": [16, 10, 6, 4, 2], "aigc": [12, 8, 5, 3, 2], "dedupe": [12, 8, 5, 3, 2]}[scope]
    cap = caps[min(index - 1, len(caps) - 1)]
    limit = max(1, min(int(max_tasks or cap), cap, 30))
    focus_limit = 2 if index >= 5 else 3 if index == 4 else 5 if index == 3 else 8

    hints: list[str] = []
    focus: list[dict] = []
    diag_info = None
    detector_before = None
    detector_error = None

    base_doc_json = ensure_doc_json(doc, base)

    if scope in ("aigc", "both"):
        diag = diag_version(doc, base)
        diag_info = {
            "riskCount": diag["result"].get("risk_count"),
            "riskScore": diag["result"].get("risk_score"),
            "level": diag["result"].get("level"),
            "gate": diag.get("gateRewritable"),
        }
        hints = build_hints(diag["result"])
        if scope == "both":
            hints.append("对篇内重复的片段做合并、回指或概括：同一内容只讲一遍，重复率不得上升")
            hints.append("合并不改变论点顺序与论据数量；不得为了消重删掉限定词")

        seg_out = Path(base["workdir"]) / "aigc" / "segdiag.json"
        try:
            for item in segdiag(base_doc_json, seg_out)[:focus_limit]:
                pid = item.get("pid")
                if pid and all(x["pid"] != pid for x in focus):
                    focus.append({**item, "source": "trace"})
        except Exception:
            pass

        if use_detector and detector_state().get("ready"):
            try:
                result = aigc_score(doc, base)
                detector_before = {
                    "aiShare": share_of(result),
                    "metric": measurement_of(result),
                    "band": result.get("band"),
                    "stability": result.get("stability"),
                    "calibrated": result.get("calibrated"),
                    "comparisonMode": result.get("comparison_mode"),
                    "top": result.get("top") or [],
                }
                for item in (result.get("top") or [])[:focus_limit]:
                    if item.get("pid") and all(x["pid"] != item["pid"] for x in focus):
                        focus.append({
                            "pid": item["pid"],
                            "tags": ["aigc_high"],
                            "risk_score": item.get("ai_prob"),
                            "chars": item.get("chars"),
                            "source": "detector",
                        })
                if detector_before["top"]:
                    hints.append("优先重写高贡献块：这些块的模型得分超过阈值且对 AI 疑似占比贡献最大，其他段落保持原意")
            except UserError as exc:
                detector_error = str(exc)
        elif use_detector:
            detector_error = "AIGC 依赖未安装，本轮只按 9 维痕迹与引擎风险选段"

    for pid in (focus_pids or [])[:focus_limit]:
        pid = str(pid).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pid) and all(x["pid"] != pid for x in focus):
            focus.append({"pid": pid, "tags": ["requested"], "source": "requested"})

    if scope in ("aigc", "both") and not hints and not focus:
        raise UserError(
            "9 维诊断与逐段考官都没有发现需要改写的段落：这篇稿子的痕迹已经很低，没必要硬改。",
            code="E_NOTHING",
            diag=diag_info,
        )
    if scope in ("aigc", "both") and not hints and focus:
        hints.append("只改被定向纳入的段落，其他段落保持原文，不改论据与结论")
    if index >= 3:
        hints = hints[:6]

    version = create_revision(doc, base, scope)
    workdir = Path(version["workdir"])
    args = [
        "rewrite",
        "--in", str(workdir / "base.docx"),
        "--outdir", str(workdir),
        "--style", "plain",
        "--max-tasks", str(limit),
    ]
    hints_path = None
    if hints:
        hints_path = workdir / ("shuangjiang-hints.md" if scope == "both" else "aigc-hints.md")
        write_text(hints_path, "\n".join(f"· {h}" for h in hints) + "\n")
        args += ["--hints", str(hints_path)]
    if focus:
        args += ["--include", ",".join(x["pid"] for x in focus[:focus_limit])]
    pacing_path = workdir / "pacing.json"
    pacing = run_proc(
        [sys.executable, str(ENGINE / "scripts" / "pacing.py"), "--doc", str(base_doc_json), "--out", str(pacing_path)],
        cwd=ENGINE,
        timeout=180,
        env_extra={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if pacing["code"] == 0 and pacing_path.exists():
        args += ["--pacing", str(pacing_path)]
        targets = (read_json(pacing_path, {}) or {}).get("targets") or {}
        for pid in targets:
            if len(focus) >= max(focus_limit, limit):
                break
            if all(x["pid"] != pid for x in focus):
                focus.append({"pid": pid, "tags": ["pacing"], "source": "pacing"})

    result = engine_run(args, timeout=300)
    tasks_path = workdir / "tasks.json"
    prompts_path = workdir / "prompts.jsonl"
    if result["code"] != 0 or not tasks_path.exists() or not prompts_path.exists():
        version["status"] = "failed"
        version["error"] = clip(result["err"] or result["out"], 700)
        save_doc(doc)
        raise UserError(f"生成改写任务失败：{version['error']}", code="E_PLAN", versionId=version["id"])

    tasks = read_json(tasks_path, {}) or {}
    prompts = [line for line in (read_text(prompts_path, "") or "").splitlines() if line.strip()]
    if not (tasks.get("tasks") or []):
        version["status"] = "failed"
        version["error"] = "引擎没有排出任何改写任务"
        save_doc(doc)
        raise UserError(
            "这一轮没有需要改写的段落：引擎按风险与重复都排不出任务。"
            "换一个模式（dedupe / aigc / both），或这篇稿子确实不用再改。",
            code="E_NOTHING",
            versionId=version["id"],
            diag=diag_info,
        )
    version.update(
        {
            "status": "planned",
            "tasks": len(tasks.get("tasks") or []),
            "prompts": len(prompts),
            "docJson": str(workdir / "base-doc.json"),
            "hints": str(hints_path) if hints_path else None,
            "focus": [x["pid"] for x in focus[:focus_limit]],
            "updatedAt": now_iso(),
        }
    )
    # rewrite 会在 workdir 里重新 inspect 基础稿，doc.json 就是基础稿的解析结果。
    if (workdir / "doc.json").exists():
        version["docJson"] = str(workdir / "doc.json")
    doc["activeVersionId"] = version["id"]
    save_doc(doc)

    return {
        "ok": True,
        "at": now_iso(),
        "docPath": doc["docPath"],
        "versionId": version["id"],
        "versionLabel": version["label"],
        "baseVersionId": base["id"],
        "baseVersionLabel": base["label"],
        "scope": scope,
        "outdir": str(workdir),
        "tasks": version["tasks"],
        "prompts": version["prompts"],
        "textStyle": tasks.get("text_style") or "plain",
        "belowThreshold": bool((tasks.get("counts") or {}).get("below_threshold")),
        "promptsPath": str(prompts_path),
        "editsPath": str(workdir / "edits.jsonl"),
        "hints": hints,
        "hintsPath": str(hints_path) if hints_path else None,
        "focus": version["focus"],
        "diag": diag_info,
        "detector": detector_before,
        "detectorError": detector_error,
        "instruction": build_instruction(version, scope),
        "constraints": CONSTRAINTS,
        "summary": {
            "tasks": version["tasks"],
            "prompts": version["prompts"],
            "focus": len(version["focus"]),
        },
    }


# ------------------------------------------------------------------ 提示词与清单


def prompt_rows(version: dict, limit: int | None = None, pid: str | None = None, skip: int = 0) -> list[dict]:
    workdir = Path(version["workdir"])
    tasks = read_json(workdir / "tasks.json", {}) or {}
    task_map = {t.get("pid"): t for t in tasks.get("tasks") or []}
    rows: list[dict] = []
    seen = set()
    for line in (read_text(workdir / "prompts.jsonl", "") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        item_pid = item.get("pid")
        if pid and item_pid != pid:
            continue
        if item_pid in seen:
            continue
        seen.add(item_pid)
        task = task_map.get(item_pid) or {}
        bounds = (task.get("constraints") or {})
        rows.append(
            {
                "pid": item_pid,
                "op": item.get("op"),
                "opName": task.get("op_name"),
                "tier": item.get("tier"),
                "risk": task.get("risk"),
                "chars": task.get("chars") or task.get("cost_chars"),
                "minChars": bounds.get("min_chars"),
                "maxChars": bounds.get("max_chars"),
                "sentences": bounds.get("sentences"),
                "targetSentences": bounds.get("target_sentences"),
                "system": item.get("system"),
                "prompt": item.get("user"),
            }
        )
        if limit and len(rows) >= skip + limit:
            break
    return rows[skip:]


def write_edits(version: dict, edits_path: str | None, edits_text: str | None) -> Path:
    workdir = Path(version["workdir"])
    target = workdir / "edits.jsonl"
    if edits_path:
        raw = Path(str(edits_path))
        source = (workdir / raw) if not raw.is_absolute() else raw
        if not source.exists():
            source = normalize_path(edits_path)
        if not source.exists():
            raise UserError(f"找不到编辑清单：{edits_path}")
        shutil.copy2(source, target)
    elif edits_text is not None:
        write_text(target, edits_text if edits_text.endswith("\n") else edits_text + "\n")
    if not target.exists():
        raise UserError(
            f"没有编辑清单：{target}\n先让模型按 prompts.jsonl 产出，或用 --text 直接贴进来。",
            code="E_NO_EDITS",
        )
    return target


def parse_edits(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate((read_text(path, "") or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise UserError(f"edits.jsonl 第 {number} 行不是合法 JSON：{clip(line, 120)}")
        if not isinstance(item, dict) or not item.get("pid") or not isinstance(item.get("text"), str):
            raise UserError(f"edits.jsonl 第 {number} 行缺 pid/text：{clip(line, 120)}")
        rows.append(item)
    if not rows:
        raise UserError("编辑清单是空的")
    return rows


def guard_edits(doc: dict, version: dict, edits: list[dict]) -> None:
    base_doc = read_json(ensure_doc_json(doc, version), {}) or {}
    known = {p.get("pid") for p in base_doc.get("paragraphs") or []}
    unknown = sorted({str(x.get("pid")) for x in edits if x.get("pid") not in known})
    if unknown:
        raise UserError(
            "清单里的段落编号不属于当前基础稿："
            + "、".join(unknown[:12])
            + f"（共 {len(unknown)} 个）。八成是把上一篇的 edits.jsonl 贴过来了。",
            code="E_FOREIGN_EDITS",
            unknown=unknown,
        )
    duplicated = sorted({str(x["pid"]) for x in edits if sum(1 for y in edits if y.get("pid") == x["pid"]) > 1})
    if duplicated:
        raise UserError(f"同一个 pid 出现多次：{'、'.join(duplicated[:8])}。每段只能有一条编辑。")


def run_lint(doc: dict, version: dict, edits_path: Path) -> dict:
    workdir = Path(version["workdir"])
    lint_path = workdir / "lint.json"
    if lint_path.exists():
        lint_path.unlink()
    result = engine_run(
        ["lint", "--in", str(version["baseDocx"]), "--outdir", str(workdir), "--edits", str(edits_path)],
        timeout=180,
    )
    lint = read_json(lint_path, {}) or {}
    if result["code"] not in (0, 1) or not lint:
        raise UserError(f"预检失败：{clip(result['err'] or result['out'], 700)}", code="E_LINT")
    rejected = [x for x in (lint.get("items") or []) if x.get("status") == "would_reject"]
    return {
        "ok": True,
        "versionId": version["id"],
        "versionLabel": version["label"],
        "counts": lint.get("counts") or {},
        "exitCode": result["code"],
        "rejected": rejected[:20],
        "reasonCounts": count_reasons(lint.get("items") or []),
        "path": str(lint_path),
    }


def count_reasons(items) -> dict:
    counts: dict[str, int] = {}
    for item in items or []:
        for reason in item.get("reasons") or []:
            code = reason.get("code") if isinstance(reason, dict) else str(reason)
            if code:
                counts[code] = counts.get(code, 0) + 1
    return counts


# ------------------------------------------------------------------ 回填与复检


def sentence_count(text: str) -> int:
    text = str(text or "")
    n = len(re.findall(r"[。！？；!?;]", text))
    return n or (1 if text.strip() else 0)


def recheck(doc: dict, version: dict, require_model: bool, repeat: bool) -> dict:
    base = version_by_id(doc, version["baseId"])
    before_diag = diag_version(doc, base)
    after_diag = diag_version(doc, version)
    b = before_diag["result"]
    a = after_diag["result"]

    new_risks = sorted(active_risks(a) - active_risks(b))
    diag_ok = (
        float(a.get("risk_score") or 0) <= float(b.get("risk_score") or 0)
        and int(a.get("risk_count") or 0) <= int(b.get("risk_count") or 0)
        and not new_risks
    )
    gate_ok = bool(after_diag.get("gateRewritable"))

    dup = None
    dup_error = None
    if repeat:
        try:
            base_dir = Path(version["workdir"]) / "check-base"
            current_dir = Path(version["workdir"]) / "check-current"
            r1 = engine_run(["diagnose", "--in", str(version["baseDocx"]), "--outdir", str(base_dir)], timeout=300)
            r2 = engine_run(["diagnose", "--in", str(version["docx"]), "--outdir", str(current_dir)], timeout=300)
            if r1["code"] != 0:
                raise UserError(f"基础稿重复率复检失败：{clip(r1['err'] or r1['out'], 200)}")
            if r2["code"] != 0:
                raise UserError(f"改稿重复率复检失败：{clip(r2['err'] or r2['out'], 200)}")
            pb = read_json(base_dir / "profile.json", {}) or {}
            pa = read_json(current_dir / "profile.json", {}) or {}
            rb = ((pb.get("duplicates") or {}).get("stats") or {}).get("repeat_rate")
            ra = ((pa.get("duplicates") or {}).get("stats") or {}).get("repeat_rate")
            if isinstance(ra, (int, float)):
                dup = {
                    "before": rb,
                    "after": ra,
                    "delta": round(rb - ra, 4) if isinstance(rb, (int, float)) else None,
                }
            else:
                dup_error = "改稿诊断没有重复率字段"
        except UserError as exc:
            dup_error = str(exc)

    prob = None
    prob_error = None
    if not require_model and not detector_state().get("ready"):
        prob_error = "未安装 torch + transformers，AIGC 复检没有执行"
    else:
        try:
            sb = aigc_score(doc, base)
            sa = aigc_score(doc, version)
            before_share = share_of(sb)
            after_share = share_of(sa)
            same_metric = measurement_of(sb) == measurement_of(sa)
            frozen = (sb.get("comparison_mode") == "frozen" and sa.get("comparison_mode") == "frozen")
            prob = {
                "mode": "model",
                "before": before_share,
                "after": after_share,
                "delta": round(before_share - after_share, 4) if (same_metric and before_share is not None and after_share is not None) else None,
                "mixedMetric": not same_metric,
                "frozenBlocks": frozen,
                "beforeBand": sb.get("band"),
                "afterBand": sa.get("band"),
                "beforeStability": sb.get("stability"),
                "afterStability": sa.get("stability"),
                "beforeAllShare": sb.get("all_ai_share"),
                "afterAllShare": sa.get("all_ai_share"),
                "beforeTableShare": sb.get("table_ai_share"),
                "afterTableShare": sa.get("table_ai_share"),
                "beforeReferenceShare": sb.get("reference_ai_share"),
                "afterReferenceShare": sa.get("reference_ai_share"),
                "beforeCalibrated": sb.get("calibrated"),
                "afterCalibrated": sa.get("calibrated"),
            }
        except UserError as exc:
            prob_error = str(exc)

    diag_ok_final = diag_ok
    repeat_ok = (float(dup["after"]) <= float(dup["before"])) if dup and isinstance(dup["before"], (int, float)) else None
    prob_ok = None
    if prob:
        prob_ok = bool(prob["frozenBlocks"]) and isinstance(prob["after"], (int, float)) and isinstance(prob["before"], (int, float)) and prob["after"] <= prob["before"]

    failed = []
    if not gate_ok:
        failed.append("语体门禁")
    if not diag_ok_final:
        failed.append("9 维风险" + (f"（新增：{'、'.join(new_risks)}）" if new_risks else ""))
    if repeat_ok is False:
        failed.append("篇内重复率")
    if dup_error:
        failed.append(f"篇内重复率未复检：{clip(dup_error, 120)}")
    if prob_ok is False:
        failed.append("正文 AI 疑似占比")
    if prob and not prob["frozenBlocks"]:
        failed.append("AIGC 复检未使用冻结块")
    if require_model and prob is None:
        failed.append("AIGC 真机依赖未安装")
    elif prob is None and prob_error and detector_state().get("ready"):
        failed.append(f"AIGC 复检失败：{clip(prob_error, 120)}")

    accepted = not failed
    return {
        "at": now_iso(),
        "versionId": version["id"],
        "versionLabel": version["label"],
        "baseVersionId": base["id"],
        "baseVersionLabel": base["label"],
        "diag": {
            "before": {
                "riskCount": b.get("risk_count"),
                "riskScore": b.get("risk_score"),
                "level": b.get("level"),
                "gate": before_diag.get("gateRewritable"),
            },
            "after": {
                "riskCount": a.get("risk_count"),
                "riskScore": a.get("risk_score"),
                "level": a.get("level"),
                "gate": after_diag.get("gateRewritable"),
            },
            "deltaScore": round(float(b.get("risk_score") or 0) - float(a.get("risk_score") or 0), 4),
            "deltaRisks": int(b.get("risk_count") or 0) - int(a.get("risk_count") or 0),
            "newRisks": new_risks,
            "todoAfter": a.get("todo") or [],
        },
        "dup": dup,
        "dupError": dup_error,
        "prob": prob,
        "probError": prob_error,
        "acceptance": {
            "accepted": accepted,
            "gate": gate_ok,
            "diag": diag_ok_final,
            "repeat": repeat_ok,
            "probability": prob_ok,
            "failed": failed,
            "aigcChecked": prob is not None,
        },
    }


def apply_edits(
    raw: str | None,
    version_target: str | None,
    edits_path: str | None,
    edits_text: str | None,
    require_model: bool,
    repeat: bool,
    auto_rollback: bool,
) -> dict:
    engine_check()
    doc = require_doc(raw)
    if doc_changed(doc):
        raise UserError("原文件在诊断后被修改了，先重新诊断再回填。", code="E_STALE")
    version = version_by_id(doc, version_target or doc.get("activeVersionId"))
    if version["id"] == "original":
        raise UserError("当前选中的是原文：先诊断（plan）出一版改稿，再回填。", code="E_ORIGINAL")

    workdir = Path(version["workdir"])
    edits = write_edits(version, edits_path, edits_text)
    rows = parse_edits(edits)
    guard_edits(doc, version, rows)

    lint = run_lint(doc, version, edits)

    for name in ("rewritten.result.json", "rewritten.verdict.json"):
        target = workdir / name
        if target.exists():
            target.unlink()
    result = engine_run(
        ["finish", "--in", str(version["baseDocx"]), "--outdir", str(workdir), "--edits", str(edits), "--style", "plain"],
        timeout=600,
    )
    verdict = read_json(workdir / "rewritten.verdict.json", {}) or {}
    applied = read_json(workdir / "rewritten.result.json", {}) or {}
    if result["code"] not in (0, 3) or not verdict or not applied:
        raise UserError(f"回填失败：{clip(result['err'] or result['out'], 800)}", code="E_APPLY")

    rewritten = workdir / "rewritten.docx"
    version_docx = workdir / "version.docx"
    if rewritten.exists():
        shutil.copy2(rewritten, version_docx)
    counts = applied.get("counts") or {}
    applied_n = int(counts.get("applied") or 0)
    rolled_back = "rolled_back" in str(applied.get("status") or "")
    structural = bool(verdict.get("passed")) and applied_n > 0 and version_docx.exists() and not rolled_back

    version.update(
        {
            "docx": str(version_docx) if structural else None,
            "docxSha": sha256_file(version_docx) if structural else None,
            "docJson": None,
            "lint": lint.get("counts"),
            "apply": counts,
            "applyStatus": applied.get("status"),
            "verdict": {
                "passed": structural,
                "changed": (verdict.get("text") or {}).get("changed_paragraphs"),
                "chars": [
                    (verdict.get("text") or {}).get("before_chars"),
                    (verdict.get("text") or {}).get("after_chars"),
                ],
                "summary": verdict.get("summary"),
            },
            "updatedAt": now_iso(),
        }
    )

    payload = {
        "ok": True,
        "at": now_iso(),
        "versionId": version["id"],
        "versionLabel": version["label"],
        "baseVersionId": version["baseId"],
        "baseVersionLabel": version["baseLabel"],
        "scope": version.get("scope"),
        "outdir": str(workdir),
        "editsPath": str(edits),
        "lint": lint.get("counts"),
        "reasonCounts": lint.get("reasonCounts"),
        "rejected": lint.get("rejected"),
        "apply": counts,
        "applyStatus": applied.get("status"),
        "noop": applied_n == 0,
        "output": str(version_docx) if structural else None,
        "verdict": version["verdict"],
        "rolledBack": not structural,
    }

    if not structural:
        version["status"] = "rejected"
        version["passed"] = False
        fallback = last_accepted(doc, version_by_id(doc, version["baseId"]))
        if auto_rollback:
            doc["activeVersionId"] = fallback["id"]
        version["rolledBackTo"] = fallback["id"]
        save_doc(doc)
        payload["rolledBackTo"] = fallback["id"]
        payload["error"] = "篇级校验未通过，本版不作为当前稿"
        return payload

    if version.get("scope") == "dedupe":
        version["status"] = "applied"
        version["passed"] = True
        version["verifiedAigc"] = None
        check = None
    else:
        check = recheck(doc, version, require_model=require_model, repeat=repeat)
        version["recheck"] = check
        version["verifiedAigc"] = bool(check["acceptance"]["aigcChecked"])
        if check["acceptance"]["accepted"]:
            version["status"] = "applied"
            version["passed"] = True
            snapshot = workdir / "accepted.docx"
            shutil.copy2(version_docx, snapshot)
            doc["activeVersionId"] = version["id"]
        else:
            version["status"] = "rejected"
            version["passed"] = False
            fallback = last_accepted(doc, version_by_id(doc, version["baseId"]))
            version["rolledBackTo"] = fallback["id"]
            if auto_rollback:
                doc["activeVersionId"] = fallback["id"]

    save_doc(doc)
    if check:
        payload["recheck"] = check
        payload["acceptance"] = check["acceptance"]
        payload["prob"] = check["prob"]
        payload["dup"] = check["dup"]
        payload["activeVersionId"] = doc["activeVersionId"]
        payload["rolledBack"] = not check["acceptance"]["accepted"]
        payload["unverified"] = not check["acceptance"]["aigcChecked"]
    else:
        payload["activeVersionId"] = doc["activeVersionId"]
    return payload


# ------------------------------------------------------------------ 对照与报告


def version_doc_json(doc: dict, version: dict) -> Path | None:
    if version["id"] == "original":
        return Path(version["workdir"]) / "doc.json"
    if not version.get("docx") or not Path(version["docx"]).exists():
        return None
    folder = Path(version["workdir"]) / "aigc"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "output-doc.json"
    result = engine_script("scripts/inspect_docx.py", ["--in", str(version["docx"]), "--out", str(out)], timeout=180)
    if result["code"] != 0 or not out.exists():
        return None
    return out


def build_compare(doc: dict, version: dict | None = None) -> dict:
    version = version or version_by_id(doc, doc.get("activeVersionId"))
    base = version_by_id(doc, version.get("baseId") or "original")
    if version["id"] == "original":
        return {"ok": True, "rows": [], "count": 0, "changed": 0, "versionId": "original"}
    base_json = ensure_doc_json(doc, base)
    after_json = version_doc_json(doc, version)
    if not after_json:
        raise UserError(f"{version['label']}还没有可对照的 docx，先回填。", code="E_NO_DOCX")
    before_doc = read_json(base_json, {}) or {}
    after_doc = read_json(after_json, {}) or {}
    after_map = {p.get("pid"): p for p in after_doc.get("paragraphs") or []}
    rows = []
    for p in before_doc.get("paragraphs") or []:
        pid = p.get("pid")
        after = after_map.get(pid) or {}
        before_text = str(p.get("text") or "")
        after_text = str(after.get("text") or "")
        changed = before_text.strip() != after_text.strip()
        rows.append(
            {
                "pid": pid,
                "kind": p.get("kind"),
                "locked": bool(p.get("locked")),
                "before": before_text,
                "after": after_text,
                "changed": changed,
                "chars": [len(before_text), len(after_text)],
                "sentences": [sentence_count(before_text), sentence_count(after_text)],
            }
        )
    changed_rows = [r for r in rows if r["changed"]]
    return {
        "ok": True,
        "versionId": version["id"],
        "versionLabel": version["label"],
        "baseVersionId": base["id"],
        "baseVersionLabel": base["label"],
        "rows": changed_rows,
        "allRows": rows,
        "count": len(changed_rows),
        "total": len(rows),
    }


def build_status(raw: str | None = None) -> dict:
    state = load_state()
    doc = None
    if raw:
        doc = state["documents"].get(doc_key(normalize_path(raw)))
    else:
        doc = state["documents"].get(state.get("activeDocKey") or "")
    det = detector_state()
    if not doc:
        return {
            "ok": True,
            "pluginVersion": PLUGIN_VERSION,
            "engine": {"dir": str(ENGINE), "ready": (ENGINE / "run.py").exists()},
            "aigc": aigc_status(),
            "doc": None,
            "runCount": len(state.get("documents") or {}),
            "message": "还没有诊断过论文",
        }
    versions = sorted(doc["versions"].values(), key=lambda v: v.get("index", 0))
    active = version_by_id(doc, doc.get("activeVersionId"))
    compare_count = 0
    try:
        compare_count = build_compare(doc, active)["count"] if active["id"] != "original" else 0
    except UserError:
        compare_count = 0
    return {
        "ok": True,
        "pluginVersion": PLUGIN_VERSION,
        "engine": {
            "dir": str(ENGINE),
            "ready": (ENGINE / "run.py").exists(),
            "python": sys.executable,
            "aigcPython": det.get("python"),
        },
        "aigc": {
            "ready": bool(det.get("ready")),
            "mode": "model" if det.get("ready") else "unavailable",
            "versions": det.get("versions"),
            "setup": aigc_setup(det) if not det.get("ready") else None,
        },
        "doc": {
            "key": doc["key"],
            "title": doc["title"],
            "path": doc["docPath"],
            "changed": doc_changed(doc),
            "converted": doc.get("converted"),
            "convertMethod": doc.get("convertMethod"),
            "createdAt": doc.get("createdAt"),
            "summary": doc.get("summary"),
            "activeVersionId": active["id"],
            "maxRevisions": MAX_REVISIONS,
            "canCreateVersion": int(active.get("index") or 0) < MAX_REVISIONS,
            "versions": [public_version(doc, v) for v in versions],
        },
        "active": {
            **public_version(doc, active),
            "chars": version_chars(active),
            "hints": active.get("hints"),
            "focus": active.get("focus") or [],
            "lint": active.get("lint"),
            "apply": active.get("apply"),
            "applyStatus": active.get("applyStatus"),
            "verdict": active.get("verdict"),
            "recheck": active.get("recheck"),
        },
        "runCount": len(state.get("documents") or {}),
        "compareCount": compare_count,
    }


# ------------------------------------------------------------------ 工作台


def workbench_payload(raw: str | None = None) -> dict:
    status = build_status(raw)
    doc_meta = status.get("doc")
    if not doc_meta:
        return {**status, "workbench": {"empty": True}, "generatedAt": now_iso()}
    doc = load_state()["documents"][doc_meta["key"]]
    active = version_by_id(doc, doc.get("activeVersionId"))
    prompts = []
    compare = {"rows": [], "allRows": [], "count": 0, "total": 0}
    diag = None
    score = None
    report = ""
    instruction = ""
    if active["id"] != "original":
        prompts = prompt_rows(active, limit=40)
        instruction = build_instruction(active, active.get("scope") or "aigc")
        try:
            compare = build_compare(doc, active)
        except UserError:
            compare = {"rows": [], "allRows": [], "count": 0, "total": 0}
        try:
            diag = diag_version(doc, active)
        except UserError:
            diag = None
        score_path = Path(active["workdir"]) / "aigc" / "score.json"
        if score_path.exists():
            score = read_json(score_path, {}) or {}
        report = read_text(Path(active["workdir"]) / "report.md", "") or ""
    else:
        try:
            diag = diag_version(doc, active)
        except UserError:
            diag = None
        report = read_text(Path(active["workdir"]) / "report.md", "") or ""

    return {
        **status,
        "generatedAt": now_iso(),
        "workbench": {
            "empty": False,
            "prompts": prompts,
            "compare": compare,
            "diag": diag,
            "score": score,
            "report": report,
            "instruction": instruction,
            "tasksPath": str(Path(active["workdir"]) / "tasks.json") if active["id"] != "original" else None,
            "promptsPath": str(Path(active["workdir"]) / "prompts.jsonl") if active["id"] != "original" else None,
            "editsPath": str(Path(active["workdir"]) / "edits.jsonl") if active["id"] != "original" else None,
        },
    }


def render_workbench(payload: dict, out_path: Path) -> Path:
    template = read_text(TEMPLATE, "")
    if not template:
        raise UserError(f"工作台模板缺失：{TEMPLATE}")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html_text = template.replace("__PAPER_DUAL_STATE__", data)
    write_text(out_path, html_text)
    return out_path


def make_workbench(raw: str | None, out: str | None) -> dict:
    payload = workbench_payload(raw)
    if not payload.get("doc"):
        raise UserError("还没有诊断过论文，工作台没有内容可放。先 diagnose。")
    target = normalize_path(out) if out else Path(load_state()["documents"][payload["doc"]["key"]]["workdir"]) / "workbench.html"
    render_workbench(payload, target)
    return {"ok": True, "path": str(target), "docPath": payload["doc"]["path"], "activeVersionId": payload["doc"]["activeVersionId"]}


# ------------------------------------------------------------------ 本地服务


class WorkbenchServer:
    def __init__(self, port: int = 8760, doc: str | None = None):
        self.port = port
        self.doc = doc
        self.lock = threading.Lock()
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PaperDualWorkbench/" + PLUGIN_VERSION

            def log_message(self, fmt, *args):
                sys.stderr.write("[workbench] " + (fmt % args) + "\n")

            def _json(self, payload, code=200):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("content-type", "application/json; charset=utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.send_header("cache-control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    return

            def _html(self, text):
                body = text.encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.send_header("cache-control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    return

            def _body(self):
                length = int(self.headers.get("content-length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return {}

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                try:
                    if path in ("/", "/index.html"):
                        payload = workbench_payload(outer.doc)
                        template = read_text(TEMPLATE, "")
                        data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
                        self._html(template.replace("__PAPER_DUAL_STATE__", data))
                        return
                    if path == "/api/state":
                        self._json(workbench_payload(outer.doc))
                        return
                    if path == "/api/report":
                        doc = require_doc(outer.doc)
                        version = version_by_id(doc, doc.get("activeVersionId"))
                        which = (query.get("which") or ["report.md"])[0]
                        allowed = {"report.md", "profile.json", "rewritten.verdict.json", "rewritten.result.json"}
                        name = which if which in allowed else "report.md"
                        self._json({"ok": True, "which": name, "text": read_text(Path(version["workdir"]) / name, "") or ""})
                        return
                    if path == "/api/download":
                        doc = require_doc(outer.doc)
                        vid = (query.get("version") or [doc.get("activeVersionId")])[0]
                        version = version_by_id(doc, vid)
                        target = version.get("docx")
                        if not target or not Path(target).exists():
                            self._json({"ok": False, "error": f"{version['label']}还没有 docx"}, 404)
                            return
                        body = Path(target).read_bytes()
                        name = f"{Path(doc['docPath']).stem}-{version['label']}.docx"
                        self.send_response(200)
                        self.send_header("content-type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                        self.send_header("content-disposition", f"attachment; filename*=UTF-8''{name}")
                        self.send_header("content-length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self._json({"ok": False, "error": f"未知接口：{path}"}, 404)
                except UserError as exc:
                    self._json({"ok": False, "error": str(exc), "code": exc.code, **exc.extra}, 400)
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 500)

            def do_POST(self):
                path = urlparse(self.path).path
                body = self._body()
                try:
                    with outer.lock:
                        if path == "/api/diagnose":
                            doc = diagnose(str(body.get("path") or ""), force=bool(body.get("force")))
                            self._json({"ok": True, "doc": build_status(doc["docPath"])})
                            return
                        if path == "/api/plan":
                            payload = plan_document(
                                outer.doc,
                                body.get("base") or None,
                                body.get("scope") if body.get("scope") in ("dedupe", "aigc", "both") else "aigc",
                                body.get("maxTasks"),
                                body.get("focus") or None,
                                body.get("useDetector") is not False,
                                False,
                            )
                            self._json(payload)
                            return
                        if path == "/api/lint":
                            doc = require_doc(outer.doc)
                            version = version_by_id(doc, body.get("version") or doc.get("activeVersionId"))
                            edits = write_edits(version, None, str(body.get("text") or ""))
                            rows = parse_edits(edits)
                            guard_edits(doc, version, rows)
                            self._json(run_lint(doc, version, edits))
                            return
                        if path == "/api/apply":
                            payload = apply_edits(
                                outer.doc,
                                body.get("version") or None,
                                None,
                                str(body.get("text") or ""),
                                bool(body.get("requireModel")),
                                body.get("repeat") is not False,
                                True,
                            )
                            self._json(payload)
                            return
                        if path == "/api/select":
                            doc = require_doc(outer.doc)
                            vid = str(body.get("versionId") or "")
                            version_by_id(doc, vid)
                            set_active(doc, vid)
                            self._json(build_status(doc["docPath"]))
                            return
                        if path == "/api/aigc":
                            doc = require_doc(outer.doc)
                            version = resolve_version(doc, body.get("version") or body.get("target"))
                            if not detector_state().get("ready"):
                                self._json(aigc_missing_payload(detector_state()), 200)
                                return
                            payload = {
                                "ok": True,
                                "versionId": version["id"],
                                "diag": diag_version(doc, version),
                                "score": aigc_score(doc, version),
                            }
                            self._json(payload)
                            return
                    self._json({"ok": False, "error": f"未知接口：{path}"}, 404)
                except UserError as exc:
                    self._json({"ok": False, "error": str(exc), "code": exc.code, **exc.extra}, 400)
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 500)

        return Handler

    def serve_forever(self):
        self.httpd.serve_forever()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"


# ------------------------------------------------------------------ 人读输出


def print_human(command: str, payload: dict) -> None:
    if command == "doctor":
        print("== paper-dual · 环境自检 ==")
        for item in payload.get("checks") or []:
            print(f"  {'✓' if item['ok'] else '✗'} {item['name']}  {item['detail']}")
        print()
        if payload.get("ok"):
            optional = [c["name"] for c in payload.get("checks") or [] if not c["ok"]]
            print("[y] 可用" + (f"（可选未装：{'、'.join(optional)}）" if optional else ""))
        else:
            print("[x] 先补齐不通过项")
        return
    if command == "status":
        if not payload.get("doc"):
            print("还没有诊断过论文。")
            return
        doc = payload["doc"]
        active = payload["active"]
        print(f"文档：{doc['title']}")
        print(f"  {doc['path']}")
        print(f"  版本：{active['label']}（{active['id']}）｜ 共 {len(doc['versions'])} 个版本")
        summary = doc.get("summary") or {}
        dist = summary.get("dist") or {}
        print(f"  风险 高/中/低：{dist.get('high', 0)}/{dist.get('mid', 0)}/{dist.get('low', 0)}"
              f" ｜ 篇内重复 {100 * float(summary.get('repeatRate') or 0):.1f}%")
        print(f"  AIGC 考官：{'就绪' if payload['aigc']['ready'] else '未安装（只做 9 维与结构验收）'}")
        return
    if command == "diagnose":
        doc = payload
        summary = doc.get("summary") or {}
        dist = summary.get("dist") or {}
        print(f"诊断完成：{doc['docPath']}")
        print(f"可改写 {summary.get('stats', {}).get('rewritable', '?')} 段"
              f" / 锁定 {summary.get('stats', {}).get('locked', '?')} 段"
              f" ｜ 高 {dist.get('high', 0)} / 中 {dist.get('mid', 0)} / 低 {dist.get('low', 0)}"
              f" ｜ 篇内重复 {100 * float(summary.get('repeatRate') or 0):.1f}%")
        if summary.get("top"):
            print("Top 段落：")
            for item in summary["top"][:5]:
                print(f"  {item['pid']} 风险 {float(item.get('risk') or 0):.2f}"
                      f" ｜ {item.get('cause')} ｜ {item.get('chars')} 字")
        print(f"报告：{Path(doc['workdir']) / 'original' / 'report.md'}")
        return
    if command == "plan":
        print(f"任务 {payload['tasks']} 段 ｜ 提示词 {payload['prompts']} 条 ｜ 文风 {payload['textStyle']}")
        print(f"版本：{payload['baseVersionLabel']} → {payload['versionLabel']}（{payload['versionId']}）")
        if payload.get("belowThreshold"):
            print("注意：全文没有段落达到风险阈值，已按相对排序取前几段")
        if payload.get("focus"):
            print(f"定向纳入：{'、'.join(payload['focus'])}")
        if payload.get("detectorError"):
            print(f"AIGC 选段：{payload['detectorError']}")
        print(f"提示词：{payload['promptsPath']}")
        print()
        print(payload["instruction"])
        return
    if command == "apply":
        lint = payload.get("lint") or {}
        apply_counts = payload.get("apply") or {}
        print(f"版本：{payload.get('baseVersionLabel')} → {payload.get('versionLabel')}（{payload.get('versionId')}）")
        print(f"预检：可通过 {lint.get('would_apply', '?')} ｜ 会被拒 {lint.get('would_reject', '?')}")
        print(f"回填：提交 {apply_counts.get('edits', '?')} ｜ 接受 {apply_counts.get('applied', '?')}"
              f" ｜ 拒绝 {apply_counts.get('rejected', '?')} ｜ 状态 {payload.get('applyStatus')}")
        verdict = payload.get("verdict") or {}
        print(f"结构复检：{'通过' if verdict.get('passed') else '未通过'}")
        acceptance = payload.get("acceptance")
        if acceptance:
            prob = payload.get("prob") or {}
            if acceptance.get("aigcChecked"):
                print(f"AIGC 复检：{'通过' if acceptance.get('accepted') else '未通过'}"
                      f" ｜ 正文 AI 疑似 {100 * float(prob.get('before') or 0):.1f}% → {100 * float(prob.get('after') or 0):.1f}%")
            else:
                print("AIGC 复检：未执行（未安装 torch + transformers；结构验收已通过）")
            if acceptance.get("failed"):
                print(f"未通过项：{'、'.join(acceptance['failed'])}")
        if payload.get("output"):
            print(f"改稿：{payload['output']}")
        if payload.get("rolledBack"):
            print(f"已回退到：{payload.get('rolledBackTo') or payload.get('activeVersionId')}")
        return
    if command == "aigc":
        if not payload.get("ok"):
            print(payload.get("error") or "AIGC 考官不可用")
            setup = payload.get("setup") or {}
            if setup.get("installScript"):
                print()
                print("安装步骤（PowerShell）：")
                print(setup["installScript"])
            return
        result = payload.get("score") or {}
        share = share_of(result)
        print(f"版本：{payload.get('versionLabel')}（{payload.get('versionId')}）")
        print(f"正文 AI 疑似占比 {100 * (share or 0):.1f}% ｜ {result.get('body_chars') or result.get('chars')} 字"
              f" ｜ {BAND_LABELS.get(result.get('band'), '未判档')}"
              f" ｜ {'已校准' if result.get('calibrated') else '未校准'}"
              f" ｜ {'冻结块' if result.get('comparison_mode') == 'frozen' else '重新切块'}")
        table_share = result.get("table_ai_share")
        table_text = "—" if table_share is None else f"{100 * float(table_share):.1f}%"
        print(f"全篇 {100 * float(result.get('all_ai_share') or 0):.1f}% ｜ 表格 {table_text}")
        diag = (payload.get("diag") or {}).get("result") or {}
        if diag:
            print(f"9 维：风险 {diag.get('risk_count')} 项 ｜ 等级 {diag.get('level')}"
                  f" ｜ 语体门禁 {'通过' if (payload.get('diag') or {}).get('gateRewritable') else '未通过'}")
        return
    if command == "lint":
        counts = payload.get("counts") or {}
        print(f"预检：可通过 {counts.get('would_apply', 0)} ｜ 会被拒 {counts.get('would_reject', 0)}")
        for item in payload.get("rejected") or []:
            print(f"  {item.get('pid')}: {item.get('reason') or ''}")
        return
    if command == "compare":
        print(f"{payload.get('versionLabel')}：改了 {payload.get('count')} 段 / 共 {payload.get('total')} 段")
        for row in (payload.get("rows") or [])[:10]:
            print(f"  {row['pid']} {row['chars'][0]}→{row['chars'][1]} 字"
                  f" ｜ {row['sentences'][0]}→{row['sentences'][1]} 句")
        return
    if command == "versions":
        for item in payload.get("versions") or []:
            mark = "*" if item["id"] == payload.get("activeVersionId") else " "
            print(f" {mark} {item['label']:<4} {item['id']:<8} {item['status']:<8}"
                  f" {'可下载' if item['downloadable'] else '无 docx'}")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ CLI


def doctor() -> dict:
    checks = []
    checks.append({"name": "python >= 3.10", "ok": sys.version_info >= (3, 10), "detail": sys.version.split()[0]})
    engine_files = ["run.py", "lib/ooxml.py", "scripts/apply_edits.py"]
    checks.append({
        "name": "引擎完整",
        "ok": all((ENGINE / f).exists() for f in engine_files),
        "detail": str(ENGINE),
    })
    checks.append({
        "name": "AIGC 考官脚本",
        "ok": (AIGC / "diag.py").exists() and (AIGC / "segscore.py").exists(),
        "detail": str(AIGC),
    })
    probe = state_root() / ".probe"
    try:
        state_root().mkdir(parents=True, exist_ok=True)
        write_text(probe, "ok")
        probe.unlink()
        writable, detail = True, str(state_root())
    except OSError as exc:
        writable, detail = False, str(exc)
    checks.append({"name": "状态目录可写", "ok": writable, "detail": detail})
    det = detector_state(force=True)
    checks.append({
        "name": "AIGC 真机依赖",
        "ok": bool(det.get("ready")),
        "detail": ("torch " + str((det.get("versions") or {}).get("torch"))) if det.get("ready")
        else "未安装（只影响 AI 疑似占比，不影响诊断/改写/回填）",
    })
    checks.append({"name": "工作台模板", "ok": TEMPLATE.exists(), "detail": str(TEMPLATE)})
    ok = all(c["ok"] for c in checks if c["name"] != "AIGC 真机依赖")
    return {"ok": ok, "checks": checks, "stateDir": str(state_root()), "pluginRoot": str(PLUGIN_ROOT), "aigc": aigc_status()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperdual.py",
        description="论文双降 · WorkBuddy 宿主层（诊断 / 改写任务 / 回填复检 / 工作台）",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出单个 JSON 对象")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[common], help="环境自检")

    p = sub.add_parser("status", parents=[common], help="当前文档与版本状态")
    p.add_argument("--doc", help="文档路径（默认最近一篇）")

    p = sub.add_parser("diagnose", parents=[common], help="诊断一篇 .doc/.docx（只读原文件）")
    p.add_argument("--in", dest="src", required=True, help="论文路径")
    p.add_argument("--force", action="store_true", help="原文件没变也重新诊断")

    p = sub.add_parser("plan", parents=[common], help="生成改写任务与提示词")
    p.add_argument("--doc", help="文档路径（默认最近一篇）")
    p.add_argument("--base", help="基础版本：original / v1 / v2 …（默认当前版本）")
    p.add_argument("--scope", choices=("dedupe", "aigc", "both"), default="aigc",
                   help="dedupe=只降重，aigc=只降 AI，both=双降（默认 aigc）")
    p.add_argument("--max-tasks", type=int, help="本轮任务上限（受轮次上限约束）")
    p.add_argument("--focus", help="强制纳入的段落 id，逗号分隔")
    p.add_argument("--no-detector", action="store_true", help="不调用 AIGC 考官选段")
    p.add_argument("--rediagnose", action="store_true", help="先重新诊断")

    p = sub.add_parser("prompts", parents=[common], help="导出本轮提示词（每段一条）")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id（默认当前版本）")
    p.add_argument("--limit", type=int, default=0, help="最多导出几条（0=全部）")
    p.add_argument("--skip", type=int, default=0, help="跳过前几条（分批改写用）")
    p.add_argument("--pid", help="只导出某一段")
    p.add_argument("--out", help="写到文件（默认打印）")

    p = sub.add_parser("lint", parents=[common], help="预检 edits.jsonl（不回填）")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id（默认当前版本）")
    p.add_argument("--edits", help="edits.jsonl 路径")
    p.add_argument("--text", help="直接给 JSONL 内容")

    p = sub.add_parser("apply", parents=[common], help="回填并复检")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id（默认当前版本）")
    p.add_argument("--edits", help="edits.jsonl 路径")
    p.add_argument("--text", help="直接给 JSONL 内容")
    p.add_argument("--require-model", action="store_true", help="没有 AIGC 真机依赖就判失败")
    p.add_argument("--no-repeat", action="store_true", help="跳过篇内重复率复检")
    p.add_argument("--no-rollback", action="store_true", help="复检不过也不切换回上一版")

    p = sub.add_parser("aigc", parents=[common], help="AIGC 考官：占比 + 9 维诊断")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id（默认当前版本）")
    p.add_argument("--calibrate", action="store_true", help="跑 0/1 标签方向标定（首次必须）")

    p = sub.add_parser("compare", parents=[common], help="三栏对照数据")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id（默认当前版本）")
    p.add_argument("--all", action="store_true", help="包含未改动段落")

    p = sub.add_parser("report", parents=[common], help="读取报告或判词")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id（默认当前版本）")
    p.add_argument("--which", default="report.md",
                   choices=("report.md", "profile.json", "rewritten.verdict.json", "rewritten.result.json"))

    p = sub.add_parser("versions", parents=[common], help="列出版本链")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--select", help="切换当前版本")

    p = sub.add_parser("export", parents=[common], help="导出某一版 docx")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--version", help="版本 id")
    p.add_argument("--out", required=True, help="目标路径")

    p = sub.add_parser("workbench", parents=[common], help="生成静态工作台 HTML")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--out", help="输出路径（默认文档产物目录/workbench.html）")

    p = sub.add_parser("serve", parents=[common], help="启动本地工作台（127.0.0.1）")
    p.add_argument("--doc", help="文档路径")
    p.add_argument("--port", type=int, default=8760, help="端口（0=随机）")

    p = sub.add_parser("config", parents=[common], help="配置解释器与模型缓存")
    p.add_argument("--python", dest="python_path", help="AIGC 考官用的 python 解释器")
    p.add_argument("--hf-cache", dest="hf_cache", help="HuggingFace 缓存目录")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(args.json)
    command = args.command

    def emit(payload: dict, code: int = 0) -> int:
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_human(command, payload)
        return code

    try:
        if command == "doctor":
            payload = doctor()
            return emit(payload, 0 if payload["ok"] else 1)

        if command == "status":
            return emit(build_status(args.doc))

        if command == "diagnose":
            doc = diagnose(args.src, force=args.force)
            return emit(doc)

        if command == "plan":
            focus = [x.strip() for x in (args.focus or "").split(",") if x.strip()]
            payload = plan_document(
                args.doc, args.base, args.scope, args.max_tasks, focus,
                not args.no_detector, args.rediagnose,
            )
            return emit(payload)

        if command == "prompts":
            doc = require_doc(args.doc)
            version = version_by_id(doc, args.version or doc.get("activeVersionId"))
            if version["id"] == "original":
                raise UserError("原文还没有提示词：先 plan 出一版改稿。", code="E_ORIGINAL")
            rows = prompt_rows(version, args.limit or None, args.pid, max(0, args.skip or 0))
            systems = []
            for row in rows:
                if row.get("system") and row["system"] not in systems:
                    systems.append(row["system"])
            slim = [{k: v for k, v in row.items() if k != "system"} for row in rows]
            payload = {
                "ok": True,
                "versionId": version["id"],
                "versionLabel": version["label"],
                "count": len(rows),
                "total": len(prompt_rows(version, None, args.pid)),
                "skip": max(0, args.skip or 0),
                "system": systems[0] if len(systems) == 1 else systems,
                "rows": slim,
                "instruction": build_instruction(version, version.get("scope") or "aigc"),
                "editsPath": str(Path(version["workdir"]) / "edits.jsonl"),
            }
            if args.out:
                target = normalize_path(args.out)
                write_json(target, payload)
                payload["path"] = str(target)
            return emit(payload)

        if command == "lint":
            doc = require_doc(args.doc)
            version = version_by_id(doc, args.version or doc.get("activeVersionId"))
            if version["id"] == "original":
                raise UserError("原文没有清单可预检。", code="E_ORIGINAL")
            edits = write_edits(version, args.edits, args.text)
            guard_edits(doc, version, parse_edits(edits))
            return emit(run_lint(doc, version, edits))

        if command == "apply":
            payload = apply_edits(
                args.doc, args.version, args.edits, args.text,
                args.require_model, not args.no_repeat, not args.no_rollback,
            )
            return emit(payload, 0 if not payload.get("rolledBack") else 1)

        if command == "aigc":
            if args.calibrate:
                return emit(aigc_calibrate())
            doc = require_doc(args.doc)
            version = resolve_version(doc, args.version)
            status = aigc_status()
            if not status["detectorReady"]:
                return emit(aigc_missing_payload(detector_state()), 1)
            diag = diag_version(doc, version)
            score = aigc_score(doc, version)
            payload = {
                "ok": True,
                "versionId": version["id"],
                "versionLabel": version["label"],
                "diag": diag,
                "score": score,
                "share": share_of(score),
                "bandLabel": BAND_LABELS.get(score.get("band"), "未判档"),
                "measurement": measurement_of(score),
                "model": AIGC_MODEL,
                "note": "这是本工具自有口径下的正文 AI 疑似占比，不是知网/维普/格子达的判定，也不能跨平台横比。",
            }
            return emit(payload)

        if command == "compare":
            doc = require_doc(args.doc)
            version = resolve_version(doc, args.version)
            payload = build_compare(doc, version)
            if not args.all:
                payload.pop("allRows", None)
            return emit(payload)

        if command == "report":
            doc = require_doc(args.doc)
            version = resolve_version(doc, args.version)
            text = read_text(Path(version["workdir"]) / args.which)
            if not text:
                raise UserError(f"{version['label']}还没有 {args.which}：先诊断或回填。", code="E_NO_REPORT")
            payload = {"ok": True, "versionId": version["id"], "versionLabel": version["label"], "which": args.which, "text": text}
            return emit(payload)

        if command == "versions":
            doc = require_doc(args.doc)
            if args.select:
                version_by_id(doc, args.select)
                set_active(doc, args.select)
                doc = require_doc(args.doc)
            items = sorted(doc["versions"].values(), key=lambda v: v.get("index", 0))
            payload = {
                "ok": True,
                "docPath": doc["docPath"],
                "activeVersionId": doc.get("activeVersionId"),
                "versions": [public_version(doc, v) for v in items],
            }
            return emit(payload)

        if command == "export":
            doc = require_doc(args.doc)
            version = resolve_version(doc, args.version)
            target = version.get("docx")
            if not target or not Path(target).exists():
                raise UserError(f"{version['label']}还没有生成 docx。", code="E_NO_DOCX")
            out = normalize_path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, out)
            return emit({"ok": True, "versionId": version["id"], "versionLabel": version["label"], "path": str(out)})

        if command == "workbench":
            return emit(make_workbench(args.doc, args.out))

        if command == "serve":
            doc = require_doc(args.doc)
            server = WorkbenchServer(args.port, doc["docPath"])
            print(f"论文双降工作台：{server.url}")
            print(f"文档：{doc['docPath']}")
            print("Ctrl+C 停止。只监听 127.0.0.1。")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")
            return 0

        if command == "config":
            patch = {}
            if args.python_path is not None:
                patch["pythonPath"] = args.python_path
            if args.hf_cache is not None:
                patch["hfCacheDir"] = args.hf_cache
            config = save_config(patch)
            _detector_cache.update({"key": None, "value": None})
            return emit({"ok": True, "config": config})

        parser.error(f"未知命令：{command}")
        return 2

    except UserError as exc:
        payload = {"ok": False, "error": str(exc), "code": exc.code, **exc.extra}
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[x] {exc}", file=sys.stderr)
            if exc.code == "AIGC_DEPS_MISSING":
                print()
                print((exc.extra.get("setup") or {}).get("installScript", ""))
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc), "code": "E_INTERNAL"}, ensure_ascii=False, indent=2))
        else:
            print(f"[x] 内部错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
