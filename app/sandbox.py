import os
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Any

try:
    import docker
    from docker.errors import NotFound as DockerNotFound

    HAS_DOCKER_SDK = True
except ImportError:
    HAS_DOCKER_SDK = False

from .db_models import GoalSession
from .db_system import get_db_session
from .secrets import (
    get_command_timeout,
    get_docker_hosts,
    get_docker_image,
    get_docker_mem_limit,
    get_projects_dir,
    get_workspace_dir,
)

IGNORE_PATTERNS: set[str] = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".DS_Store",
}
EXCLUDED_EXTENSIONS: tuple[str, ...] = (".pyc", ".tmp", ".log")


# --- ワークスペース & パス検証 ---
def is_safe_path(target_path: Path, base_dir: Path) -> bool:
    try:
        resolved = target_path.resolve()
        return base_dir in resolved.parents or resolved == base_dir
    except Exception:
        return False


def resolve_safe_workspace_file(filename: str, workspace_dir: Path) -> Path | None:
    clean_name = filename.strip().replace("~/", "").lstrip("/")
    redundant_prefixes = [
        str(workspace_dir).lstrip("/"),
        str(get_workspace_dir()).lstrip("/"),
        "workspace/",
    ]
    for prefix in redundant_prefixes:
        if clean_name.startswith(prefix):
            clean_name = clean_name[len(prefix) :].lstrip("/")

    if not clean_name:
        return None

    target_file = (workspace_dir / clean_name).resolve()
    return target_file if is_safe_path(target_file, workspace_dir) else None


def get_target_workspace(
    session_id: int | None = None,
    base_dir: Path | None = None,
) -> Path:
    if base_dir and base_dir.exists():
        return base_dir

    if session_id is not None:
        with get_db_session() as db:
            sess = db.query(GoalSession).filter(GoalSession.id == session_id).first()
            if sess and sess.project_dir:
                project_path = Path(sess.project_dir).resolve()
                project_path.mkdir(parents=True, exist_ok=True)
                return project_path

        fallback_path = get_projects_dir() / f"session_{session_id}"
        fallback_path.mkdir(parents=True, exist_ok=True)
        return fallback_path

    return get_workspace_dir()


def package_project_to_zip(project_dir: Path, session_id: int) -> Path:
    zip_path = project_dir.parent / f"session_{session_id}_archive.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
            for file in files:
                if file.endswith(EXCLUDED_EXTENSIONS):
                    continue
                file_path = Path(root) / file
                arcname = file_path.relative_to(project_dir)
                zipf.write(file_path, arcname)
    return zip_path


# --- Docker サンドボックス抽象化クラス (スケールアウト対応) ---
class DockerSandboxManager:
    """Docker SDK を用いてローカルおよびリモート Docker ノードのコンテナを統合管理する．"""

    def __init__(self) -> None:
        self._session_host_map: dict[int, str] = {}

    @staticmethod
    def get_container_name(session_id: int | None) -> str:
        return f"runner_session_{session_id or 'default'}"

    def _get_sdk_client(self, session_id: int | None) -> Any:
        """最も負荷の低い Docker ホストを選択して DockerClient を返す．"""
        if not HAS_DOCKER_SDK:
            return None

        hosts = get_docker_hosts()
        if session_id is not None and session_id in self._session_host_map:
            target_host = self._session_host_map[session_id]
            return docker.DockerClient(base_url=target_host, timeout=30)

        best_client = None
        best_host = hosts[0]
        min_containers = float("inf")

        for host_url in hosts:
            try:
                client = docker.DockerClient(base_url=host_url, timeout=10)
                running_count = len(client.containers.list())
                if running_count < min_containers:
                    min_containers = running_count
                    best_client = client
                    best_host = host_url
            except Exception:
                continue

        if best_client is None:
            best_client = docker.from_env(timeout=30)
        elif session_id is not None:
            self._session_host_map[session_id] = best_host

        return best_client

    def ensure_container(self, session_id: int | None, project_dir: Path) -> str:
        """サンドボックスコンテナの稼働を確認し，未起動ならリソース制限付きで起動する．"""
        container_name = self.get_container_name(session_id)
        client = self._get_sdk_client(session_id)

        if client is not None:
            try:
                container = client.containers.get(container_name)
                if container.status == "running":
                    return container_name
                container.remove(force=True)
            except DockerNotFound:
                pass
            except Exception:
                pass

            client.containers.run(
                image=get_docker_image(),
                command=["tail", "-f", "/dev/null"],
                name=container_name,
                detach=True,
                working_dir="/workspace",
                mem_limit=get_docker_mem_limit(),
                volumes={
                    str(project_dir.resolve()): {"bind": "/workspace", "mode": "rw"}
                },
            )
            return container_name

        # CLI フォールバック
        return self._ensure_container_cli(container_name, project_dir)

    def execute_command(
        self, session_id: int | None, workspace_dir: Path, command: str
    ) -> str:
        """コンテナ内でタイムアウト監視付きでコマンドを実行する．"""
        timeout_sec = get_command_timeout()
        container_name = self.ensure_container(session_id, workspace_dir)
        client = self._get_sdk_client(session_id)

        if client is not None:
            try:
                container = client.containers.get(container_name)
                result_holder: dict[str, Any] = {}

                def _run_exec() -> None:
                    try:
                        exit_code, output_bytes = container.exec_run(
                            cmd=["sh", "-c", command],
                            workdir="/workspace",
                            demux=False,
                        )
                        result_holder["output"] = (
                            output_bytes.decode("utf-8", errors="replace").strip()
                            if output_bytes
                            else ""
                        )
                    except Exception as exc:
                        result_holder["error"] = str(exc)

                exec_thread = threading.Thread(target=_run_exec, daemon=True)
                exec_thread.start()
                exec_thread.join(timeout=timeout_sec)

                if exec_thread.is_alive():
                    container.restart(timeout=2)
                    return (
                        f"Error: Command timed out ({timeout_sec}s limit). "
                        "Container restarted to clear hanging processes."
                    )

                if "error" in result_holder:
                    return f"Execution error in sandbox: {result_holder['error']}"

                output = result_holder.get("output", "")
                git_commit_checkpoint(workspace_dir, f"run_command: {command[:50]}")
                return output or "(execution completed with no output)"
            except Exception as exc:
                return f"Execution error in Docker SDK sandbox: {exc}"

        return self._execute_command_cli(container_name, workspace_dir, command, timeout_sec)

    def stop_container(self, session_id: int | None) -> None:
        """セッション終了時にコンテナを破棄し，ホスト割り当て情報を解放する．"""
        container_name = self.get_container_name(session_id)
        client = self._get_sdk_client(session_id)

        if session_id is not None:
            self._session_host_map.pop(session_id, None)

        if client is not None:
            try:
                container = client.containers.get(container_name)
                container.remove(force=True)
                return
            except Exception:
                pass

        subprocess.run(
            ["docker", "rm", "-f", container_name],
            check=False,
            capture_output=True,
        )

    # --- Docker SDK 未導入環境用の CLI フォールバック ---
    def _ensure_container_cli(self, container_name: str, project_dir: Path) -> str:
        check = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            check=False,
            capture_output=True,
            text=True,
        )
        if check.returncode == 0 and "true" in check.stdout.lower():
            return container_name

        subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-m", get_docker_mem_limit(),
            "-v", f"{project_dir.resolve()}:/workspace",
            "-w", "/workspace",
            get_docker_image(),
            "tail", "-f", "/dev/null",
        ]
        res = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Docker container start failed: {res.stderr}")
        return container_name

    def _execute_command_cli(
        self, container_name: str, workspace_dir: Path, command: str, timeout_sec: int
    ) -> str:
        docker_exec_cmd = [
            "docker", "exec", "-w", "/workspace", container_name, "sh", "-c", command
        ]
        try:
            proc = subprocess.Popen(
                docker_exec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_sec)
                output = (stdout + stderr).strip()
                git_commit_checkpoint(workspace_dir, f"run_command: {command[:50]}")
                return output or "(execution completed with no output)"
            except subprocess.TimeoutExpired:
                proc.kill()
                self._ensure_container_cli(container_name, workspace_dir)
                return f"Error: Command timed out ({timeout_sec}s limit). Process killed."
        except Exception as exc:
            return f"Execution error in sandbox: {exc}"


# シングルトンマネージャと互換ラッパー関数
sandbox_manager = DockerSandboxManager()


def exec_in_docker(session_id: int | None, workspace_dir: Path, command: str) -> str:
    return sandbox_manager.execute_command(session_id, workspace_dir, command)


def stop_docker_container(session_id: int | None) -> None:
    sandbox_manager.stop_container(session_id)


# --- Git チェックポイント管理 ---
def _run_git(project_dir: Path, *git_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *git_args],
        check=False,
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )


def init_git_repo(project_dir: Path) -> None:
    try:
        _run_git(project_dir, "init")
        _run_git(project_dir, "config", "user.name", "Agent")
        _run_git(project_dir, "config", "user.email", "agent@sandbox.local")
        _run_git(project_dir, "add", "-A")
        _run_git(project_dir, "commit", "-m", "Initial commit", "--allow-empty")
    except Exception:
        pass


def git_commit_checkpoint(project_dir: Path, message: str) -> None:
    try:
        _run_git(project_dir, "add", "-A")
        _run_git(project_dir, "commit", "-m", message)
    except Exception:
        pass


def git_diff_head(project_dir: Path) -> str:
    res = _run_git(project_dir, "diff", "HEAD~1")
    return res.stdout.strip() or "(no diff with previous commit)"


def git_revert_head(project_dir: Path) -> str:
    res = _run_git(project_dir, "reset", "--hard", "HEAD~1")
    if res.returncode == 0:
        return "Successfully reverted to previous commit checkpoint."
    return f"Revert failed: {res.stderr}"