import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"


def reload_env() -> None:
    load_dotenv(dotenv_path=ENV_PATH, override=True)


def _get_str(key: str, default: str = "") -> str:
    reload_env()
    return os.getenv(key, default)


def _get_int(key: str, default: int) -> int:
    reload_env()
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def load_token() -> str:
    return _get_str("DISCORD_TOKEN", "")


def get_llama_server_url() -> str:
    return _get_str(
        "LLAMA_SERVER_URL", "http://192.168.10.119:8080/v1/chat/completions"
    )


def get_llama_embedding_url() -> str:
    """Embedding 取得用エンドポイント（未指定時は chat URL から自動推定）．"""
    custom_url = _get_str("LLAMA_EMBEDDING_URL", "")
    if custom_url:
        return custom_url
    return get_llama_server_url().replace("/chat/completions", "/embeddings")


def get_max_token() -> int:
    return _get_int("MAX_TOKEN", 4096)


def get_server_ctx_size() -> int:
    return _get_int("SERVER_CTX_SIZE", 16384)


def get_max_steps() -> int:
    return _get_int("MAX_STEPS", 40)


def get_command_timeout() -> int:
    return _get_int("COMMAND_TIMEOUT", 60)


def get_docker_image() -> str:
    return _get_str("DOCKER_IMAGE", "python:3.12-slim")


def get_docker_hosts() -> list[str]:
    """カンマ区切りの DOCKER_HOSTS をリストとして返す（スケールアウト用）．"""
    raw = _get_str("DOCKER_HOSTS", "unix:///var/run/docker.sock")
    return [h.strip() for h in raw.split(",") if h.strip()]


def get_docker_mem_limit() -> str:
    return _get_str("DOCKER_MEM_LIMIT", "512m")


def get_max_concurrent_tasks() -> int:
    """同時に実行を許可するエージェントの最大ワーカー数．"""
    return max(1, _get_int("MAX_CONCURRENT_TASKS", 2))


def get_rag_top_k() -> int:
    return _get_int("RAG_TOP_K", 3)


def get_workspace_dir() -> Path:
    custom_ws = _get_str("WORKSPACE_DIR")
    if custom_ws:
        workspace_path = Path(custom_ws).expanduser().resolve()
    else:
        workspace_path = (Path.home() / "discord-bot" / "workspace").resolve()

    workspace_path.mkdir(parents=True, exist_ok=True)
    return workspace_path


def get_projects_dir() -> Path:
    projects_path = get_workspace_dir() / "projects"
    projects_path.mkdir(parents=True, exist_ok=True)
    return projects_path