import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from ddgs import DDGS

from .db_models import GoalSession, Planner
from .db_system import SessionLocal
from .secret import (
    get_command_timeout,
    get_docker_image,
    get_projects_dir,
    get_workspace_dir,
)

IGNORE_PATTERNS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".DS_Store",
}

AVAILABLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command inside the isolated Docker sandbox workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command string to execute (e.g. 'python app.py', 'pip install requests').",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read content from a file inside the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max lines to read (default 150).",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with full text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Raw text content.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Precisely replace specific text snippet inside an existing file without rewriting whole content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text snippet to be replaced.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "New replacement text.",
                    },
                },
                "required": ["filename", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_into_file",
            "description": "Insert a code snippet before or after a specific target marker in an existing file. Perfect for adding <script> tags or JavaScript functions without rewriting the entire file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the target file (e.g. 'index.html').",
                    },
                    "target_marker": {
                        "type": "string",
                        "description": "The exact unique string to locate the insertion point (e.g. '</body>', '</script>', or a comment '// INSERT_HERE').",
                    },
                    "content": {
                        "type": "string",
                        "description": "The code snippet or text content to insert.",
                    },
                    "position": {
                        "type": "string",
                        "enum": ["before", "after"],
                        "description": "Whether to insert 'before' or 'after' the target_marker (default is 'before').",
                    },
                },
                "required": ["filename", "target_marker", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and subdirectories inside the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default is root '.').",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_troubleshooting",
            "description": (
                "Save a troubleshooting memo to the database after resolving an"
                " unexpected error or roadblock. Future tasks will reference"
                " this knowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "Short identifier (e.g."
                            " 'docker_env','port_conflict','syntax_error')."
                        ),
                    },
                    "error_pattern": {
                        "type": "string",
                        "description": "The error text or symptoms faced.",
                    },
                    "root_cause": {
                        "type": "string",
                        "description": (
                            "Explanation of why the error occurred."
                        ),
                    },
                    "solution": {
                        "type": "string",
                        "description": "How to resolve or avoid this error.",
                    },
                },
                "required": [
                    "category",
                    "error_pattern",
                    "root_cause",
                    "solution",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Inspect git diff to see recent code modifications since last checkpoint.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_revert",
            "description": "Revert workspace to the previous git commit checkpoint if recent changes caused unexpected failure.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for up-to-date documentation, solutions to unfamiliar errors, or libraries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query terms.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch text content from a web URL for documentation or reference.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full HTTP/HTTPS URL.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": "Ask the human user for clarification, required API keys, or design choices when requirements are ambiguous.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Clear and polite question in Japanese addressed to the user.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_plan",
            "description": "Record the current issue, planned task, and anticipated future steps into the persistent Planner DB before executing complex operations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_issue": {
                        "type": "string",
                        "description": "What problem are we solving right now?",
                    },
                    "task": {
                        "type": "string",
                        "description": "Specific action plan to execute.",
                    },
                    "future_issue": {
                        "type": "string",
                        "description": "Next challenges or backlog to tackle later.",
                    },
                },
                "required": ["current_issue", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_result",
            "description": "Update the latest plan record with actual results and refined future issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "Outcome of the executed task.",
                    },
                    "future_issue": {
                        "type": "string",
                        "description": "Updated next issues based on what was learned.",
                    },
                },
                "required": ["result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Declare the task finished. The 'result' argument MUST be written in Japanese summarizing accomplishments and deliverables.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "Final summary and explanation written in Japanese.",
                    }
                },
                "required": ["result"],
            },
        },
    },
]


def is_safe_path(target_path: Path, base_dir: Path) -> bool:
    try:
        resolved = target_path.resolve()
        return base_dir in resolved.parents or resolved == base_dir
    except Exception:
        return False


def get_target_workspace(session_id: int | None = None, base_dir: Path | None = None) -> Path:
    if base_dir and base_dir.exists():
        return base_dir

    if session_id:
        db = SessionLocal()
        try:
            sess = db.query(GoalSession).filter(GoalSession.id == session_id).first()
            if sess and sess.project_dir:
                p = Path(sess.project_dir).resolve()
                p.mkdir(parents=True, exist_ok=True)
                return p
        finally:
            db.close()
        p = get_projects_dir() / f"session_{session_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    return get_workspace_dir()


# --- Docker サンドボックス管理 ---
def get_container_name(session_id: int) -> str:
    return f"runner_session_{session_id}"


def ensure_docker_container(session_id: int, project_dir: Path) -> str:
    container_name = get_container_name(session_id)
    # コンテナが起動中か確認
    check = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
        check=False, capture_output=True,
        text=True,
    )
    if check.returncode == 0 and "true" in check.stdout.lower():
        return container_name

    # 停止中・異常状態のコンテナをクリーンアップ
    subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)

    image = get_docker_image()
    # /workspace にマウントしてバックグラウンド待機
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "-v",
        f"{project_dir.resolve()}:/workspace",
        "-w",
        "/workspace",
        image,
        "tail",
        "-f",
        "/dev/null",
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Docker container start failed: {res.stderr}")
    return container_name


def stop_docker_container(session_id: int):
    container_name = get_container_name(session_id)
    subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)


# --- Git チェックポイント管理 ---
def init_git_repo(project_dir: Path):
    try:
        subprocess.run(["git", "init"], check=False, cwd=str(project_dir), capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "Agent"],
            check=False, cwd=str(project_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "agent@sandbox.local"],
            check=False, cwd=str(project_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "-A"], check=False, cwd=str(project_dir), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit", "--allow-empty"],
            check=False, cwd=str(project_dir),
            capture_output=True,
        )
    except Exception:
        pass


def git_commit_checkpoint(project_dir: Path, message: str):
    try:
        subprocess.run(
            ["git", "add", "-A"], check=False, cwd=str(project_dir), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            check=False, cwd=str(project_dir),
            capture_output=True,
        )
    except Exception:
        pass


# --- Web 検索 & 取得 ---
def perform_web_search(query: str, max_results: int = 5) -> str:
    # クエリに日本語が含まれているか簡易判定
    is_japanese = any(ord(char) > 0x3000 for char in query)
    region = "jp-jp" if is_japanese else "wt-wt"

    try:
        with DDGS() as ddgs:
            # 日本語なら jp-jp を指定して検索
            results = list(
                ddgs.text(query, region=region, max_results=max_results)
            )

            # ヒットしなかった場合はグローバルリージョンで再試行
            if not results and is_japanese:
                results = list(
                    ddgs.text(query, region="wt-wt", max_results=max_results)
                )

        if not results:
            return "No clear search results found. Try a shorter query (e.g. 1-2 keywords)."

        formatted = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            snippet = r.get("body", "")
            link = r.get("href", "")
            formatted.append(f"{i}. {title}\n   {snippet}\n   URL: {link}")

        return "\n".join(formatted)

    except Exception as e:
        return f"Search error: {e}"


def fetch_web_page(target_url: str) -> str:
    req = urllib.request.Request(
        target_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        # script, style を除去
        content = re.sub(
            r"<(script|style).*?>.*?</\1>", "", content, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(r"<[^>]+>", " ", content)
        text = " ".join(text.split())
        return text[:3000] + ("\n...(truncated)..." if len(text) > 3000 else "")
    except Exception as e:
        return f"Fetch error: {e}"


# --- ツール実行ディスパッチャ ---
async def execute_tool_async(
    name: str,
    args: dict | None,
    session_id: int | None = None,
    project_dir: Path | None = None,
    human_ask_callback=None,
) -> str:
    workspace_dir = get_target_workspace(session_id, project_dir)
    args = args or {}

    if name == "write_file":
        content = str(args.get("content", ""))
        filename = str(args.get("filename", "")).strip()
        if not filename:
            return "Error: 'filename' parameter is required."

        clean_filename = filename.replace("~/", "").lstrip("/")
        redundant_prefixes = [
            str(workspace_dir).lstrip("/"),
            str(get_workspace_dir()).lstrip("/"),
            "workspace/",
        ]
        for prefix in redundant_prefixes:
            if clean_filename.startswith(prefix):
                clean_filename = clean_filename[len(prefix) :].lstrip("/")

        target_file = (workspace_dir / clean_filename).resolve()
        if not is_safe_path(target_file, workspace_dir):
            return "Error: Access denied. Cannot write outside project workspace."

        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(content, encoding="utf-8")
            git_commit_checkpoint(workspace_dir, f"write_file: {clean_filename}")
            return f"Successfully wrote {len(content)} characters to {clean_filename}."
        except Exception as e:
            return f"Error writing file: {e}"

    elif name == "replace_in_file":
        filename = str(args.get("filename", "")).strip()
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))

        if not filename or not old_text:
            return "Error: 'filename' and 'old_text' are required."

        clean_filename = filename.replace("~/", "").lstrip("/")
        target_file = (workspace_dir / clean_filename).resolve()
        if not is_safe_path(target_file, workspace_dir) or not target_file.exists():
            return f"Error: File '{clean_filename}' does not exist or access denied."

        try:
            file_content = target_file.read_text(encoding="utf-8")
            count = file_content.count(old_text)
            if count == 0:
                return f"Error: 'old_text' was not found in {clean_filename}. Verify file content."
            if count > 1:
                return (
                    f"Warning: 'old_text' occurred {count} times. Please include more surrounding "
                    f"context in 'old_text' to make it unique."
                )

            updated = file_content.replace(old_text, new_text, 1)
            target_file.write_text(updated, encoding="utf-8")
            git_commit_checkpoint(workspace_dir, f"replace_in_file: {clean_filename}")
            return f"Successfully replaced snippet in {clean_filename}."
        except Exception as e:
            return f"Error updating file: {e}"

    elif name == "insert_into_file":
        filename = str(args.get("filename", "")).strip()
        target_marker = str(args.get("target_marker", ""))
        content = str(args.get("content", ""))
        position = str(args.get("position", "before")).strip().lower()

        if not filename or not target_marker:
            return "Error: 'filename' and 'target_marker' are required."

        clean_filename = filename.replace("~/", "").lstrip("/")
        target_file = (workspace_dir / clean_filename).resolve()

        if (
            not is_safe_path(target_file, workspace_dir)
            or not target_file.exists()
        ):
            return f"Error: File '{clean_filename}' does not exist or access denied."

        try:
            file_text = target_file.read_text(encoding="utf-8")
            marker_count = file_text.count(target_marker)

            if marker_count == 0:
                return (
                    f"Error: Marker '{target_marker}' not found in {clean_filename}. "
                    "Use 'read_file' to confirm the exact anchor text."
                )
            if marker_count > 1:
                return (
                    f"Warning: Marker '{target_marker}' found {marker_count} times. "
                    "Please specify a more unique marker (or include nearby surrounding text/comments)."
                )

            # 挿入位置の算出
            idx = file_text.find(target_marker)
            if position == "after":
                insert_pos = idx + len(target_marker)
                # 改行を自然に補完
                insertion = (
                    ("\n" if not content.startswith("\n") else "")
                    + content
                    + ("\n" if not content.endswith("\n") else "")
                )
            else:  # 'before'
                insert_pos = idx
                insertion = (
                    ("\n" if not content.startswith("\n") else "")
                    + content
                    + ("\n" if not content.endswith("\n") else "")
                )

            new_file_text = (
                file_text[:insert_pos] + insertion + file_text[insert_pos:]
            )
            target_file.write_text(new_file_text, encoding="utf-8")

            # Git チェックポイント（もしGit自動コミットを導入している場合）
            if "git_commit_checkpoint" in globals():
                git_commit_checkpoint(
                    workspace_dir, f"insert_into_file: {clean_filename}"
                )

            return f"Successfully inserted {len(content)} characters {position} '{target_marker}' in {clean_filename}."
        except Exception as e:
            return f"Error inserting into file: {e}"

    elif name == "read_file":
        filename = str(args.get("filename", "")).strip()
        clean_filename = filename.replace("~/", "").lstrip("/")
        target_file = (workspace_dir / clean_filename).resolve()

        if not is_safe_path(target_file, workspace_dir) or not target_file.exists():
            return f"Error: File '{clean_filename}' not found."

        try:
            max_lines = int(args.get("max_lines", 150))
            lines = target_file.read_text(encoding="utf-8", errors="replace").splitlines()
            result = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                result += f"\n...(truncated after {max_lines} of {len(lines)} lines)..."
            return result or "(file is empty)"
        except Exception as e:
            return f"Error reading file: {e}"

    elif name == "list_files":
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

    elif name == "git_diff":
        res = subprocess.run(
            ["git", "diff", "HEAD~1"],
            check=False, cwd=str(workspace_dir),
            capture_output=True,
            text=True,
        )
        return res.stdout.strip() or "(no diff with previous commit)"

    elif name == "git_revert":
        res = subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            check=False, cwd=str(workspace_dir),
            capture_output=True,
            text=True,
        )
        return (
            "Successfully reverted to previous commit checkpoint."
            if res.returncode == 0
            else f"Revert failed: {res.stderr}"
        )

    elif name == "web_search":
        q = str(args.get("query", "")).strip()
        return perform_web_search(q)

    elif name == "fetch_url":
        url = str(args.get("url", "")).strip()
        return fetch_web_page(url)

    elif name == "ask_human":
        question = str(args.get("question", "")).strip()
        if not human_ask_callback:
            return "Human interaction is unavailable in this environment."
        answer = await human_ask_callback(question)
        return f"Human response: {answer}"

    elif name == "run_command":
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return "Error: 'command' parameter is required."

        timeout_sec = get_command_timeout()
        container_name = ensure_docker_container(session_id, workspace_dir)

        # Docker コンテナ内でコマンドを安全に実行
        docker_exec_cmd = [
            "docker",
            "exec",
            "-w",
            "/workspace",
            container_name,
            "sh",
            "-c",
            cmd,
        ]

        try:
            proc = subprocess.Popen(
                docker_exec_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_sec)
                out = (stdout + stderr).strip()
                git_commit_checkpoint(workspace_dir, f"run_command: {cmd[:50]}")
                return out or "(execution completed with no output)"
            except subprocess.TimeoutExpired:
                proc.kill()
                # タイムアウトした場合はコンテナを安全に再起動
                ensure_docker_container(session_id, workspace_dir)
                return f"Error: Command timed out ({timeout_sec}s limit). Process killed."
        except Exception as e:
            return f"Execution error in sandbox: {e}"

    elif name == "record_plan":
        db = SessionLocal()
        try:
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
        except Exception as e:
            db.rollback()
            return f"Error recording plan: {e}"
        finally:
            db.close()

    elif name == "update_plan_result":
        db = SessionLocal()
        try:
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
        except Exception as e:
            db.rollback()
            return f"Error updating plan: {e}"
        finally:
            db.close()

    elif name == "finish_task":
        return str(args.get("result", ""))

    return f"Error: Unknown tool {name}"
