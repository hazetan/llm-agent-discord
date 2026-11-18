import asyncio
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from ddgs import DDGS

from src.core.config import get_workspace_dir

from .container import (
    IGNORE_DIRS,
    export_workspace_zip,
    resolve_workspace_path,
    run_in_sandbox,
)

INITIAL_DOCUMENTS: dict[str, str] = {
    "README.md": (
        "# Workspace Master Ledger (~/README.md)\n\n"
        "## ディレクトリ構成・プロジェクト一覧\n"
        "- 現在登録されているサブプロジェクトはありません。\n"
    ),
    "TASK.md": (
        "# Project WBS & Task Queue (~/TASK.md)\n\n"
        "<!-- Status tags: [TODO] (設計待ち), [IMPL] (実装待ち), [QA] (検証待ち), [FIX] (差戻し), [DONE] (完了) -->\n"
        "- 現在未完了のタスクはありません。\n"
    ),
    "DESIGN.md": (
        "# Architecture & Detailed Design (~/DESIGN.md)\n\n"
        "## 設計仕様\n"
        "- SEによる基本設計・詳細設計がここに記述されます。\n"
    ),
    "REPORT.md": (
        "# PMO Internal Audit Report (~/REPORT.md)\n\n"
        "## 監査・品質改善フィードバック\n"
        "- 現在特記すべき問題はありません。\n"
    ),
}


def init_workspace_files(reset_on_restart: bool = False) -> None:
    """起動時に ~/ 内の社内標準ドキュメント4点を整備する"""
    ws = get_workspace_dir()
    for name, template in INITIAL_DOCUMENTS.items():
        path = ws / name
        if reset_on_restart or not path.exists():
            path.write_text(template, encoding="utf-8")


def read_workspace_doc(name: str) -> str:
    path = resolve_workspace_path(name)
    if path and path.exists() and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _tool_list_files(args: dict[str, Any]) -> str:
    raw_dir = str(args.get("directory", "~/"))
    target_dir = resolve_workspace_path(raw_dir)
    if not target_dir or not target_dir.exists() or not target_dir.is_dir():
        return f"Error: Directory '{raw_dir}' does not exist inside ~/."

    ws = get_workspace_dir().resolve()
    lines: list[str] = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        rel_root = Path(root).relative_to(ws)
        prefix = "~/" if str(rel_root) == "." else f"~/{rel_root}/"
        for d in dirs:
            lines.append(f"[DIR]  {prefix}{d}/")
        for f in sorted(files):
            lines.append(f"[FILE] {prefix}{f}")
    return "\n".join(lines) if lines else "(Directory is empty)"


def _tool_read_file(args: dict[str, Any]) -> str:
    raw_path = str(args.get("path", "")).strip()
    target = resolve_workspace_path(raw_path)
    if not target or not target.exists():
        return f"Error: File '{raw_path}' not found inside ~/."
    if target.is_dir():
        return f"Error: '{raw_path}' is a directory. Use 'list_files' instead."

    max_lines = int(args.get("max_lines", 250))
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        body += f"\n...(truncated after {max_lines} of {len(lines)} lines)..."
    return body or "(Empty file)"


def _tool_write_file(args: dict[str, Any]) -> str:
    raw_path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    target = resolve_workspace_path(raw_path)
    if not target or target.is_dir():
        return f"Error: Invalid file path '{raw_path}'."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = target.relative_to(get_workspace_dir().resolve())
    return f"Successfully wrote {len(content)} chars to ~/{rel}."


def _tool_replace_in_file(args: dict[str, Any]) -> str:
    raw_path = str(args.get("path", "")).strip()
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    target = resolve_workspace_path(raw_path)
    if not target or not target.exists() or target.is_dir():
        return f"Error: File '{raw_path}' not found."

    text = target.read_text(encoding="utf-8")
    if old_text not in text:
        return f"Error: 'old_text' not found in {raw_path}."
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    rel = target.relative_to(get_workspace_dir().resolve())
    return f"Successfully updated ~/{rel}."


def _tool_run_command(args: dict[str, Any]) -> str:
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "Error: Empty command."
    return run_in_sandbox(cmd)


def _tool_web_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: Empty query."
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if not results:
            return "No results found."
        return "\n".join(
            f"{i}. {r.get('title')}\n   {r.get('body')}\n   URL: {r.get('href')}"
            for i, r in enumerate(results, 1)
        )
    except Exception as exc:
        return f"Search error: {exc}"


def _tool_fetch_url(args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        text = re.sub(r"<(script|style).*?>.*?</\1>", "", html, flags=re.DOTALL | re.I)
        text = " ".join(re.sub(r"<[^>]+>", " ", text).split())
        return text[:4000]
    except Exception as exc:
        return f"Fetch error: {exc}"


def _tool_export_zip(args: dict[str, Any]) -> str:
    sub = str(args.get("directory", "~/"))
    zip_path = export_workspace_zip(sub)
    return f"ZIP_CREATED:{zip_path}"


TOOL_DISPATCH = {
    "list_files": _tool_list_files,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "replace_in_file": _tool_replace_in_file,
    "run_command": _tool_run_command,
    "web_search": _tool_web_search,
    "fetch_url": _tool_fetch_url,
    "export_zip": _tool_export_zip,
}


def parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except Exception:
            return {}
    return {}


async def execute_tool(name: str, raw_args: Any) -> str:
    args = parse_tool_args(raw_args)
    if name == "complete_phase":
        return str(args.get("summary", "Phase completed."))
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return f"Error: Unknown tool '{name}'."
    return await asyncio.to_thread(fn, args)