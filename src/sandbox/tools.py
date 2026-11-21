import asyncio
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from ddgs import DDGS

from core.config import get_io_chunk_bytes, get_max_ctx_chars_budget, get_workspace_dir

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


SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg", ".mp3", ".mp4", ".wav",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".pdf", ".woff", ".woff2", ".ttf", ".eot",
    ".db", ".sqlite", ".sqlite3", ".pkl", ".pt", ".pth", ".onnx", ".safetensors", ".gguf", ".lock",
}


def _is_readable_text_file(path: Path, allowed_exts: set[str] | None = None) -> bool:
    """バイナリ・対象外拡張子のみを除外し、巨大テキストファイルもエラーにせず通す"""
    if not path.is_file():
        return False
    ext = path.suffix.lower()
    if allowed_exts is not None:
        return ext in allowed_exts
    if ext in SKIP_EXTENSIONS:
        return False
    try:
        with path.open("rb") as f:
            chunk = f.read(512)
            if b"\x00" in chunk:
                return False
    except Exception:
        return False
    return True


def _stream_read_lines(
    path: Path,
    start_line: int = 1,
    end_line: int | None = None,
    max_chars: int | None = None,
) -> tuple[list[str], int, bool]:
    """
    粒度可変のバッファサイズ (IO_CHUNK_BYTES) でファイルをストリーミング走査し、
    指定行範囲 (start_line〜end_line) かつ文字数予算内の行リスト・総行数・予算到達有無を返す。
    """
    buf_size = get_io_chunk_bytes()
    char_limit = max_chars or int(get_max_ctx_chars_budget() * 0.7)

    collected: list[str] = []
    total_lines = 0
    acc_chars = 0
    budget_reached = False

    with path.open("r", encoding="utf-8", errors="replace", buffering=buf_size) as f:
        for idx, raw_line in enumerate(f, start=1):
            total_lines = idx
            if idx < start_line:
                continue
            if end_line is not None and idx > end_line:
                continue
            if not budget_reached:
                line = raw_line.rstrip("\r\n")
                if acc_chars + len(line) + 12 > char_limit and collected:
                    budget_reached = True
                else:
                    collected.append(line)
                    acc_chars += len(line) + 12

    return collected, total_lines, budget_reached


def _format_with_line_numbers(lines: list[str], start_line: int = 1) -> str:
    """各行の左側に1始まりの行番号 ('   1 | ...') を付与する"""
    if not lines:
        return "(Empty file)"
    width = max(4, len(str(start_line + len(lines) - 1)))
    return "\n".join(f"{idx:>{width}} | {line}" for idx, line in enumerate(lines, start=start_line))


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
        return f"Error: '{raw_path}' is a directory. Use 'read_files' or 'list_files' instead."

    start_line = max(1, int(args.get("start_line", 1)))
    max_lines = int(args.get("max_lines", 2000))
    req_end_line = int(args.get("end_line", start_line + max_lines - 1))
    char_budget = int(get_max_ctx_chars_budget() * 0.7)

    sliced, total, budget_hit = _stream_read_lines(
        target, start_line=start_line, end_line=req_end_line, max_chars=char_budget
    )
    rel = target.relative_to(get_workspace_dir().resolve())
    if total == 0:
        return f"=== FILE: ~/{rel} (0 lines) ===\n(Empty file)"

    actual_end = start_line + len(sliced) - 1 if sliced else start_line
    header = f"=== FILE: ~/{rel} (lines {start_line}-{actual_end} of {total}) ==="
    body = _format_with_line_numbers(sliced, start_line=start_line)
    if actual_end < total:
        reason = "256k context safety budget reached" if budget_hit else f"{total - actual_end} more lines"
        body += f"\n...({reason}; call read_file with start_line={actual_end + 1} to read further)..."
    return f"{header}\n{body}"



def _tool_read_files(args: dict[str, Any]) -> str:
    """複数ファイルまたはディレクトリ配下を粒度可変ストリーミング＋行番号付きで一括閲覧する"""
    ws = get_workspace_dir().resolve()
    raw_paths = args.get("paths") or []
    if isinstance(raw_paths, str):
        raw_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]

    raw_dir = args.get("directory")
    if not raw_paths and not raw_dir:
        raw_dir = "~/"

    raw_exts = args.get("extensions")
    allowed_exts: set[str] | None = None
    if raw_exts:
        if isinstance(raw_exts, str):
            raw_exts = [e.strip() for e in raw_exts.split(",") if e.strip()]
        allowed_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in raw_exts}

    max_lines_per_file = int(args.get("max_lines_per_file", 1000))
    batch_char_budget = int(get_max_ctx_chars_budget() * 0.7)
    collected: list[Path] = []
    seen: set[Path] = set()

    def _add_candidate(p: Path) -> None:
        resolved = p.resolve()
        if resolved not in seen and _is_readable_text_file(resolved, allowed_exts):
            seen.add(resolved)
            collected.append(resolved)

    def _walk_dir(d: Path) -> None:
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if x not in IGNORE_DIRS and not x.startswith("."))
            for f in sorted(files):
                _add_candidate(Path(root) / f)

    for rp in raw_paths:
        target = resolve_workspace_path(str(rp))
        if not target or not target.exists():
            continue
        if target.is_dir():
            _walk_dir(target)
        else:
            _add_candidate(target)

    if raw_dir:
        target_dir = resolve_workspace_path(str(raw_dir))
        if target_dir and target_dir.exists():
            if target_dir.is_dir():
                _walk_dir(target_dir)
            else:
                _add_candidate(target_dir)

    if not collected:
        return "No readable text files matched the specified paths/directory."

    sections: list[str] = []
    total_chars = 0
    omitted_files: list[str] = []

    for idx, fpath in enumerate(collected):
        remaining_budget = max(4000, batch_char_budget - total_chars)
        if total_chars >= batch_char_budget and sections:
            omitted_files = [f"~/{p.relative_to(ws)}" for p in collected[idx:]]
            break

        rel = fpath.relative_to(ws)
        shown_lines, total_lines, budget_hit = _stream_read_lines(
            fpath, start_line=1, end_line=max_lines_per_file, max_chars=remaining_budget
        )
        body = _format_with_line_numbers(shown_lines, start_line=1)
        if len(shown_lines) < total_lines:
            body += (
                f"\n...(truncated after {len(shown_lines)} of {total_lines} lines; "
                f"use read_file with start_line={len(shown_lines) + 1} if needed)..."
            )

        block = f"=== FILE: ~/{rel} (total {total_lines} lines) ===\n{body}"
        sections.append(block)
        total_chars += len(block)

        if budget_hit and idx + 1 < len(collected):
            omitted_files = [f"~/{p.relative_to(ws)}" for p in collected[idx + 1 :]]
            break

    if omitted_files:
        sections.append(
            f"=== OMITTED FILES (reached 256k context safety budget of {batch_char_budget:,} chars) ===\n"
            + "\n".join(f"- {p}" for p in omitted_files)
        )

    return "\n\n".join(sections)


def _tool_write_file(args: dict[str, Any]) -> str:
    raw_path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    target = resolve_workspace_path(raw_path)
    if not target or target.is_dir():
        return f"Error: Invalid file path '{raw_path}'."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = target.relative_to(get_workspace_dir().resolve())
    line_cnt = len(content.splitlines())
    return f"Successfully wrote {len(content)} chars ({line_cnt} lines) to ~/{rel}."


def _tool_write_files(args: dict[str, Any]) -> str:
    """複数ファイルを一括で新規作成・上書きする"""
    files_list = args.get("files") or []
    if not isinstance(files_list, list) or not files_list:
        return "Error: 'files' must be a non-empty list of {'path': ..., 'content': ...}."

    results: list[str] = []
    for item in files_list:
        if not isinstance(item, dict):
            continue
        results.append(_tool_write_file(item))
    return "\n".join(results) if results else "Error: No valid file entries provided."


def _tool_edit_lines(args: dict[str, Any]) -> str:
    """
    行番号範囲 (start_line 〜 end_line, 1始まり・両端含む) を指定して改行付き文字列に一括置換する。
    同一ファイル内の複数箇所編集は行番号の降順（下から上）で自動適用し、行ズレを防止する。
    """
    raw_edits = args.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        edit_items = raw_edits
    elif "path" in args and "start_line" in args:
        edit_items = [args]
    else:
        return "Error: Provide 'edits' list or ('path', 'start_line', 'end_line', 'new_text')."

    ws = get_workspace_dir().resolve()
    grouped: dict[Path, list[tuple[int, int, str]]] = {}
    errors: list[str] = []

    for idx, item in enumerate(edit_items, 1):
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path", "")).strip()
        target = resolve_workspace_path(raw_path)
        if not target or not target.exists() or target.is_dir():
            errors.append(f"Edit #{idx}: File '{raw_path}' not found.")
            continue
        try:
            s_line = int(item["start_line"])
            e_line = int(item.get("end_line", s_line))
        except Exception:
            errors.append(f"Edit #{idx} ({raw_path}): 'start_line'/'end_line' must be integers.")
            continue

        if s_line < 1 or e_line < s_line:
            errors.append(f"Edit #{idx} ({raw_path}): Invalid range {s_line}-{e_line} (must be 1 <= start_line <= end_line).")
            continue

        new_text = str(item.get("new_text", item.get("new_content", "")))
        grouped.setdefault(target, []).append((s_line, e_line, new_text))

    if errors:
        return "Error(s) in edit_lines:\n" + "\n".join(errors)

    reports: list[str] = []

    for target, ops in grouped.items():
        rel = target.relative_to(ws)
        orig_text = target.read_text(encoding="utf-8", errors="replace")
        lines = orig_text.splitlines()
        total = len(lines)

        # 下の行（start_lineの降順）からソート
        ops_sorted = sorted(ops, key=lambda x: (x[0], x[1]), reverse=True)

        # 範囲妥当性＆重複チェック
        file_err = False
        for i, (s_line, e_line, _) in enumerate(ops_sorted):
            if s_line > total + 1 or e_line > max(total, s_line):
                reports.append(f"Error (~/{rel}): Range {s_line}-{e_line} exceeds file length ({total} lines).")
                file_err = True
                break
            if i > 0:
                prev_s, prev_e, _ = ops_sorted[i - 1]
                if e_line >= prev_s:
                    reports.append(
                        f"Error (~/{rel}): Overlapping line ranges {s_line}-{e_line} and {prev_s}-{prev_e}."
                    )
                    file_err = True
                    break
        if file_err:
            continue

        # 降順で置換適用
        for s_line, e_line, new_text in ops_sorted:
            replacement_lines = new_text.splitlines() if new_text != "" else []
            lines[s_line - 1 : e_line] = replacement_lines

        new_full_text = "\n".join(lines) + ("\n" if lines else "")
        target.write_text(new_full_text, encoding="utf-8")

        # 編集後のプレビュー（最初の編集箇所の周辺）を生成
        min_s = min(op[0] for op in ops_sorted)
        max_e = max(op[1] for op in ops_sorted)
        preview_start = max(1, min_s - 2)
        preview_end = min(len(lines), max_e + 5)
        preview_body = _format_with_line_numbers(lines[preview_start - 1 : preview_end], start_line=preview_start)

        reports.append(
            f"Updated ~/{rel} ({len(ops_sorted)} edit(s) applied, now {len(lines)} lines). "
            f"Preview (lines {preview_start}-{preview_end}):\n{preview_body}"
        )

    return "\n\n".join(reports)


def _tool_replace_in_file(args: dict[str, Any]) -> str:
    raw_path = str(args.get("path", "")).strip()
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    target = resolve_workspace_path(raw_path)
    if not target or not target.exists() or target.is_dir():
        return f"Error: File '{raw_path}' not found."

    text = target.read_text(encoding="utf-8")
    if old_text not in text:
        return f"Error: 'old_text' not found in {raw_path}. Use 'read_file' and 'edit_lines' with line numbers instead."
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    rel = target.relative_to(get_workspace_dir().resolve())
    return f"Successfully updated ~/{rel}."


def _tool_search_files(args: dict[str, Any]) -> str:
    """IGNORE_DIRS・バイナリを除外し、ストリーミング走査でキーワード／正規表現を横断検索する"""
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: Empty 'query'."

    raw_dir = str(args.get("directory", "~/"))
    target_dir = resolve_workspace_path(raw_dir)
    if not target_dir or not target_dir.exists():
        return f"Error: Directory '{raw_dir}' does not exist."

    raw_exts = args.get("extensions")
    allowed_exts: set[str] | None = None
    if raw_exts:
        if isinstance(raw_exts, str):
            raw_exts = [e.strip() for e in raw_exts.split(",") if e.strip()]
        allowed_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in raw_exts}

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    ws = get_workspace_dir().resolve()
    buf_size = get_io_chunk_bytes()
    matches: list[str] = []
    match_count = 0

    for root, dirs, files in os.walk(target_dir):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith("."))
        for fname in sorted(files):
            fpath = Path(root) / fname
            if not _is_readable_text_file(fpath, allowed_exts):
                continue
            hit_lines: list[str] = []
            try:
                with fpath.open("r", encoding="utf-8", errors="replace", buffering=buf_size) as f:
                    for idx, raw_line in enumerate(f, 1):
                        line = raw_line.rstrip("\r\n")
                        if pattern.search(line):
                            hit_lines.append(f"{idx:>4} | {line}")
                            match_count += 1
                            if len(hit_lines) >= 30:
                                hit_lines.append("  ...(more matches in this file)...")
                                break
            except Exception:
                continue
            if hit_lines:
                rel = fpath.relative_to(ws)
                matches.append(f"=== ~/{rel} ===\n" + "\n".join(hit_lines))
            if match_count >= 200:
                break
        if match_count >= 200:
            matches.append("...(search limit of 200 matches reached)...")
            break

    return "\n\n".join(matches) if matches else f"No matches found for '{query}'."


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
    "read_files": _tool_read_files,
    "write_file": _tool_write_file,
    "write_files": _tool_write_files,
    "edit_lines": _tool_edit_lines,
    "replace_in_file": _tool_replace_in_file,
    "search_files": _tool_search_files,
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


_file_write_lock = asyncio.Lock()


async def execute_tool(name: str, raw_args: Any) -> str:
    args = parse_tool_args(raw_args)
    if name == "complete_phase":
        return str(args.get("summary", "Phase completed."))
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return f"Error: Unknown tool '{name}'."

    if name in ("write_file", "write_files", "edit_lines", "replace_in_file"):
        async with _file_write_lock:
            return await asyncio.to_thread(fn, args)

    return await asyncio.to_thread(fn, args)


def build_heavy_context_bundle(context_paths: list[str]) -> str:
    """
    Stage 2 用に、指定されたファイル・ディレクトリ群をPythonで高速に一括読み込みし、
    行番号なしのクリーンなテキストバンドルとして結合する。
    """
    ws = get_workspace_dir().resolve()
    char_budget = int(get_max_ctx_chars_budget(is_heavy=True) * 0.65)
    collected: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        r = p.resolve()
        if r not in seen and _is_readable_text_file(r):
            seen.add(r)
            collected.append(r)

    for raw_p in context_paths:
        target = resolve_workspace_path(str(raw_p))
        if not target or not target.exists():
            continue
        if target.is_dir():
            for root, dirs, files in os.walk(target):
                dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith("."))
                for f in sorted(files):
                    _add(Path(root) / f)
        else:
            _add(target)

    if not collected:
        return "(追加の参照ファイルなし)"

    sections: list[str] = []
    used_chars = 0
    for fpath in collected:
        rel = fpath.relative_to(ws)
        text = fpath.read_text(encoding="utf-8", errors="replace")
        if used_chars + len(text) > char_budget and sections:
            sections.append(f"=== ~/{rel} (Skipped: Heavy context budget reached) ===")
            continue
        sections.append(f"=== FILE: ~/{rel} ===\n{text}")
        used_chars += len(text)

    return "\n\n".join(sections)


HEAVY_FILE_HEADER_PATTERN = re.compile(
    r"(?:^|\n)[ \t]*(?:#+|\*\*|-)?[ \t]*FILE[ \t]*:[ \t]*([^\n]+)",
)


def auto_apply_heavy_markdown(markdown_text: str) -> list[str]:
    """
    Stage 3 (Auto-Apply): 重量モデルが出力した Markdown から
    '### FILE: ~/path/to/file' セクションを分割し、内部にネストしたコードブロックが
    あっても途切れないようセクション内の最初と最後の ``` の間を抽出して自動保存する。
    """
    ws = get_workspace_dir().resolve()
    applied_reports: list[str] = []

    matches = list(HEAVY_FILE_HEADER_PATTERN.finditer(markdown_text))
    for idx, match in enumerate(matches):
        raw_path = match.group(1).strip().strip("`'\"* ")
        section_start = match.end()
        section_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
        section = markdown_text[section_start:section_end]

        first_fence = re.search(r"```[^\n]*\n", section)
        if not first_fence:
            continue
        content_start = first_fence.end()
        last_fence_pos = section.rfind("```")
        if last_fence_pos <= first_fence.start():
            code_body = section[content_start:]
        else:
            code_body = section[content_start:last_fence_pos]

        target = resolve_workspace_path(raw_path)
        if not target or target.is_dir():
            applied_reports.append(f"[ERROR] Invalid target path skipped: {raw_path}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code_body, encoding="utf-8")
        rel = target.relative_to(ws)
        line_cnt = len(code_body.splitlines())
        applied_reports.append(f"~/{rel} ({line_cnt} lines, {len(code_body)} chars)")

    return applied_reports