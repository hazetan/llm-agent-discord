import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db_models import Planner
from .db_system import get_db_session
from .prompts_and_schemas import AVAILABLE_TOOLS
from .rag_store import rag_store
from .sandbox import (
    IGNORE_PATTERNS,
    exec_in_docker,
    get_target_workspace,
    git_commit_checkpoint,
    git_diff_head,
    git_revert_head,
    init_git_repo,
    is_safe_path,
    resolve_safe_workspace_file,
    stop_docker_container,
)
from .web_utils import fetch_web_page, perform_web_search

__all__ = [
    "AVAILABLE_TOOLS",
    "IGNORE_PATTERNS",
    "execute_tool_async",
    "init_git_repo",
    "stop_docker_container",
]


def _tool_write_file(args: dict[str, Any], workspace_dir: Path, _: int | None) -> str:
    filename = str(args.get("filename", "")).strip()
    content = str(args.get("content", ""))
    if not filename:
        return "Error: 'filename' parameter is required."

    target_file = resolve_safe_workspace_file(filename, workspace_dir)
    if not target_file:
        return "Error: Access denied. Cannot write outside project workspace."

    try:
        rel_name = target_file.relative_to(workspace_dir)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        git_commit_checkpoint(workspace_dir, f"write_file: {rel_name}")
        return f"Successfully wrote {len(content)} characters to {rel_name}."
    except Exception as exc:
        return f"Error writing file: {exc}"


def _tool_replace_in_file(args: dict[str, Any], workspace_dir: Path, _: int | None) -> str:
    filename = str(args.get("filename", "")).strip()
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))

    if not filename or not old_text:
        return "Error: 'filename' and 'old_text' are required."

    target_file = resolve_safe_workspace_file(filename, workspace_dir)
    if not target_file or not target_file.exists():
        return f"Error: File '{filename}' does not exist or access denied."

    try:
        rel_name = target_file.relative_to(workspace_dir)
        file_content = target_file.read_text(encoding="utf-8")
        count = file_content.count(old_text)

        if count == 0:
            return f"Error: 'old_text' was not found in {rel_name}. Verify file content."
        if count > 1:
            return (
                f"Warning: 'old_text' occurred {count} times. Please include more "
                "surrounding context in 'old_text' to make it unique."
            )

        updated = file_content.replace(old_text, new_text, 1)
        target_file.write_text(updated, encoding="utf-8")
        git_commit_checkpoint(workspace_dir, f"replace_in_file: {rel_name}")
        return f"Successfully replaced snippet in {rel_name}."
    except Exception as exc:
        return f"Error updating file: {exc}"


def _tool_insert_into_file(args: dict[str, Any], workspace_dir: Path, _: int | None) -> str:
    filename = str(args.get("filename", "")).strip()
    target_marker = str(args.get("target_marker", ""))
    content = str(args.get("content", ""))
    position = str(args.get("position", "before")).strip().lower()

    if not filename or not target_marker:
        return "Error: 'filename' and 'target_marker' are required."

    target_file = resolve_safe_workspace_file(filename, workspace_dir)
    if not target_file or not target_file.exists():
        return f"Error: File '{filename}' does not exist or access denied."

    try:
        rel_name = target_file.relative_to(workspace_dir)
        file_text = target_file.read_text(encoding="utf-8")
        marker_count = file_text.count(target_marker)

        if marker_count == 0:
            return (
                f"Error: Marker '{target_marker}' not found in {rel_name}. "
                "Use 'read_file' to confirm the exact anchor text."
            )
        if marker_count > 1:
            return (
                f"Warning: Marker '{target_marker}' found {marker_count} times. "
                "Please specify a more unique marker."
            )

        idx = file_text.find(target_marker)
        insert_pos = idx + len(target_marker) if position == "after" else idx

        prefix_nl = "\n" if not content.startswith("\n") else ""
        suffix_nl = "\n" if not content.endswith("\n") else ""
        insertion = f"{prefix_nl}{content}{suffix_nl}"

        new_file_text = file_text[:insert_pos] + insertion + file_text[insert_pos:]
        target_file.write_text(new_file_text, encoding="utf-8")
        git_commit_checkpoint(workspace_dir, f"insert_into_file: {rel_name}")
        return f"Successfully inserted {len(content)} characters {position} '{target_marker}' in {rel_name}."
    except Exception as exc:
        return f"Error inserting into file: {exc}"


def _tool_read_file(args: dict[str, Any], workspace_dir: Path, _: int | None) -> str:
    filename = str(args.get("filename", "")).strip()
    if not filename:
        return "Error: 'filename' parameter is required."

    target_file = resolve_safe_workspace_file(filename, workspace_dir)
    if not target_file or not target_file.exists():
        return f"Error: File '{filename}' not found."
    if target_file.is_dir():
        return f"Error: '{filename}' is a directory. Use 'list_files' instead."

    try:
        max_lines = int(args.get("max_lines", 150))
        lines = target_file.read_text(encoding="utf-8", errors="replace").splitlines()
        result = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            result += f"\n...(truncated after {max_lines} of {len(lines)} lines)..."
        return result or "(file is empty)"
    except Exception as exc:
        return f"Error reading file: {exc}"


def _tool_list_files(args: dict[str, Any], workspace_dir: Path, _: int | None) -> str:
    req_dir = str(args.get("directory", ".")).strip()
    target_dir = (workspace_dir / req_dir).resolve()
    if not is_safe_path(target_dir, workspace_dir) or not target_dir.exists():
        return f"Error: Directory '{req_dir}' not found."

    tree_lines = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
        rel_root = Path(root).relative_to(workspace_dir)
        prefix = "" if str(rel_root) == "." else f"{rel_root}/"
        for d in dirs:
            tree_lines.append(f"📁 {prefix}{d}/")
        for f in files:
            tree_lines.append(f"📄 {prefix}{f}")
    return "\n".join(tree_lines) if tree_lines else "(directory is empty)"


def _tool_run_command(
    args: dict[str, Any], workspace_dir: Path, session_id: int | None
) -> str:
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "Error: 'command' parameter is required."
    return exec_in_docker(session_id, workspace_dir, cmd)


def _tool_git_diff(_: dict[str, Any], workspace_dir: Path, __: int | None) -> str:
    return git_diff_head(workspace_dir)


def _tool_git_revert(_: dict[str, Any], workspace_dir: Path, __: int | None) -> str:
    return git_revert_head(workspace_dir)


def _tool_web_search(args: dict[str, Any], _: Path, __: int | None) -> str:
    query = str(args.get("query", "")).strip()
    return perform_web_search(query)


def _tool_fetch_url(args: dict[str, Any], _: Path, __: int | None) -> str:
    url = str(args.get("url", "")).strip()
    return fetch_web_page(url)


def _tool_record_plan(
    args: dict[str, Any], _: Path, session_id: int | None
) -> str:
    try:
        with get_db_session() as db:
            plan = Planner(
                session_id=session_id,
                current_issue=str(args.get("current_issue", "")),
                task=str(args.get("task", "")),
                future_issue=str(args.get("future_issue", "")),
                status="in_progress",
            )
            db.add(plan)
            db.commit()
            return f"Plan recorded successfully (ID: {plan.id})."
    except Exception as exc:
        return f"Error recording plan: {exc}"


def _tool_update_plan_result(
    args: dict[str, Any], _: Path, session_id: int | None
) -> str:
    try:
        with get_db_session() as db:
            latest_plan = (
                db.query(Planner)
                .filter(Planner.session_id == session_id)
                .order_by(Planner.id.desc())
                .first()
            )
            if not latest_plan:
                return "Error: No existing plan record found to update."

            latest_plan.result = str(args.get("result", ""))
            if "future_issue" in args:
                latest_plan.future_issue = str(args.get("future_issue"))
            latest_plan.status = "completed"
            db.commit()
            return f"Plan ID {latest_plan.id} updated successfully."
    except Exception as exc:
        return f"Error updating plan: {exc}"


def _tool_record_troubleshooting(
    args: dict[str, Any], _: Path, __: int | None
) -> str:
    """エラー解決の知見を Embedding ベクトル付きで RAG ストアへ登録する．"""
    try:
        memo_id = rag_store.add_memory_sync(
            category=str(args.get("category", "general")),
            error_pattern=str(args.get("error_pattern", "")),
            root_cause=str(args.get("root_cause", "")),
            solution=str(args.get("solution", "")),
        )
        return f"Troubleshooting memo indexed in RAG vector store (ID: {memo_id})."
    except Exception as exc:
        return f"Error saving troubleshooting memo: {exc}"


def _tool_finish_task(args: dict[str, Any], _: Path, __: int | None) -> str:
    return str(args.get("result", ""))


SYNC_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], Path, int | None], str]] = {
    "write_file": _tool_write_file,
    "replace_in_file": _tool_replace_in_file,
    "insert_into_file": _tool_insert_into_file,
    "read_file": _tool_read_file,
    "list_files": _tool_list_files,
    "run_command": _tool_run_command,
    "git_diff": _tool_git_diff,
    "git_revert": _tool_git_revert,
    "web_search": _tool_web_search,
    "fetch_url": _tool_fetch_url,
    "record_plan": _tool_record_plan,
    "update_plan_result": _tool_update_plan_result,
    "record_troubleshooting": _tool_record_troubleshooting,
    "finish_task": _tool_finish_task,
}


async def execute_tool_async(
    name: str,
    args: dict[str, Any] | None,
    session_id: int | None = None,
    project_dir: Path | None = None,
    human_ask_callback: Callable | None = None,
) -> str:
    workspace_dir = get_target_workspace(session_id, project_dir)
    safe_args = args or {}

    if name == "ask_human":
        question = str(safe_args.get("question", "")).strip()
        if not human_ask_callback:
            return "Human interaction is unavailable in this environment."
        answer = await human_ask_callback(question)
        return f"Human response: {answer}"

    handler = SYNC_TOOL_HANDLERS.get(name)
    if not handler:
        return f"Error: Unknown tool {name}"

    return handler(safe_args, workspace_dir, session_id)