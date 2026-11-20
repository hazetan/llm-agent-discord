
import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from core.config import (
    get_command_timeout,
    get_docker_image,
    get_docker_mem_limit,
    get_exports_dir,
    get_io_chunk_bytes,
    get_workspace_dir,
)

CONTAINER_NAME = "sier_agent_sandbox"
CONTAINER_HOME = "/root"
IGNORE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    "node_modules",
    "build",
    "dist",
    "target",
    "out",
    ".next",
    ".nuxt",
    ".cache",
    "coverage",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "bin",
    "obj",
}


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


def ensure_sandbox_container(recreate: bool = False) -> str:
    """単一のサンドボックスコンテナに workspace_dir を /root (~/) としてマウントし起動する"""
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
        return CONTAINER_NAME

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
        raise RuntimeError(f"Failed to start Docker container ({CONTAINER_NAME}): {res.stderr}")
    return CONTAINER_NAME


def run_in_sandbox(command: str) -> str:
    """
    サンドボックスコンテナ内の /root (~/) でコマンドを実行する。
    コマンド文字列はチャンク粒度可変の標準入力 (stdin) ストリーミングで渡すため、
    256kサイズ等の巨大コマンドでも OS の ARG_MAX (128KB) エラーにならない。
    """
    c_name = ensure_sandbox_container(recreate=False)
    timeout_sec = get_command_timeout()
    chunk_size = get_io_chunk_bytes()
    uid = os.getuid()
    gid = os.getgid()

    inner_script = (
        f"{command}\n"
        f"EXIT_CODE=$?\n"
        f"chown -R {uid}:{gid} {CONTAINER_HOME} 2>/dev/null\n"
        f"exit $EXIT_CODE\n"
    )
    script_bytes = inner_script.encode("utf-8", errors="replace")

    proc = subprocess.Popen(
        [
            "docker", "exec", "-i",
            "-w", CONTAINER_HOME,
            c_name,
            "timeout", "-k", "5", f"{timeout_sec}s",
            "sh",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _feed_stdin() -> None:
        if not proc.stdin:
            return
        try:
            view = memoryview(script_bytes)
            for offset in range(0, len(script_bytes), chunk_size):
                proc.stdin.write(view[offset : offset + chunk_size])
                proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feeder = threading.Thread(target=_feed_stdin, daemon=True)
    feeder.start()

    out_chunks: list[bytes] = []
    try:
        if proc.stdout:
            while True:
                buf = proc.stdout.read(chunk_size)
                if not buf:
                    break
                out_chunks.append(buf)
        proc.wait(timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired:
        proc.kill()
        return f"Error: Command timed out after {timeout_sec}s."
    finally:
        feeder.join(timeout=2)

    out = b"".join(out_chunks).decode("utf-8", errors="replace").strip()
    if proc.returncode in (124, 137):
        return f"Error: Command timed out after {timeout_sec}s.\n{out}"
    if proc.returncode != 0:
        return f"[Exit Code: {proc.returncode}]\n{out}"
    return out or "(Command succeeded with no output)"


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