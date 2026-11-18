import os
import shlex
import subprocess
import time
import zipfile
from pathlib import Path

from src.core.config import (
    get_command_timeout,
    get_docker_image,
    get_docker_mem_limit,
    get_exports_dir,
    get_workspace_dir,
)

CONTAINER_NAME = "sier_agent_sandbox"
CONTAINER_HOME = "/root"
IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", "node_modules"}


def resolve_workspace_path(raw_path: str) -> Path | None:
    """
    エージェントが指定したあらゆるパス表記 ('~/a/README.md', '/root/a/README.md',
    'workspace/a/README.md', './a/README.md', 'a/README.md') を
    ホスト側の workspace_dir 配下の安全な絶対パスへ正規化する。
    """
    workspace_dir = get_workspace_dir().resolve()
    cleaned = raw_path.strip()

    if not cleaned or cleaned in ("~", "~/", ".", "./", "/root", "/root/", "/workspace", "workspace"):
        return workspace_dir

    # 先頭の仮想ホーム・ワークスペースプレフィックスを順番に剥がす
    prefixes = (
        str(workspace_dir),
        "/root/",
        "/workspace/",
        "~/workspace/",
        "~/",
        "./",
        "workspace/",
    )
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break

    cleaned = cleaned.lstrip("/")
    target = (workspace_dir / cleaned).resolve()

    # ディレクトリトラバーサル防止チェック
    if target == workspace_dir or workspace_dir in target.parents:
        return target
    return None


def ensure_sandbox_container(recreate: bool = False) -> None:
    """workspace_dir をコンテナ内の /root (~/) にマウントして起動する"""
    workspace_dir = get_workspace_dir().resolve()
    if recreate:
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=False, capture_output=True)

    check = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if check.returncode == 0 and "true" in check.stdout.lower():
        return

    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=False, capture_output=True)
    cmd = [
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "-e", f"HOME={CONTAINER_HOME}",
        "-v", f"{workspace_dir}:{CONTAINER_HOME}",
        "-w", CONTAINER_HOME,
    ]
    mem_limit = get_docker_mem_limit()
    if mem_limit:
        cmd.extend(["-m", mem_limit])

    cmd.extend([get_docker_image(), "tail", "-f", "/dev/null"])
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to start Docker container: {res.stderr}")


def run_in_sandbox(command: str) -> str:
    """コンテナ内の /root (~/) でコマンドを実行し、生成ファイルの所有権をホストユーザーに揃える"""
    ensure_sandbox_container(recreate=False)
    timeout_sec = get_command_timeout()
    uid = os.getuid()
    gid = os.getgid()

    # コマンド実行後、終了コードを維持したまま /root 配下の所有権をホストの UID:GID に戻す
    inner_script = f"{command}; EXIT_CODE=$?; chown -R {uid}:{gid} {CONTAINER_HOME} 2>/dev/null; exit $EXIT_CODE"
    wrapped = f"timeout -k 5 {timeout_sec}s sh -c {shlex.quote(inner_script)}"

    try:
        res = subprocess.run(
            ["docker", "exec", "-w", CONTAINER_HOME, CONTAINER_NAME, "sh", "-c", wrapped],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 10,
        )
        out = (res.stdout + res.stderr).strip()
        if res.returncode in (124, 137):
            return f"Error: Command timed out after {timeout_sec}s.\n{out}"
        if res.returncode != 0:
            return f"[Exit Code: {res.returncode}]\n{out}"
        return out or "(Command succeeded with no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout_sec}s."


def export_workspace_zip(sub_path: str = ".") -> Path:
    """workspace (または指定サブフォルダ) を exports/ フォルダ内にZIP化する"""
    target_dir = resolve_workspace_path(sub_path) or get_workspace_dir()
    exports_dir = get_exports_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    label = target_dir.name if target_dir != get_workspace_dir() else "workspace"
    zip_path = exports_dir / f"{label}_{timestamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for file in files:
                if file.endswith((".pyc", ".tmp")):
                    continue
                full_path = Path(root) / file
                zf.write(full_path, full_path.relative_to(target_dir))
    return zip_path