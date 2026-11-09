from os import getenv
from os.path import abspath, dirname, join
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = join(dirname(dirname(abspath(__file__))), ".env")


def reload_env():
    load_dotenv(dotenv_path=ENV_PATH, override=True)


def load_token() -> str:
    reload_env()
    return getenv("DISCORD_TOKEN", "")


def get_llama_server_url() -> str:
    reload_env()
    return getenv(
        "LLAMA_SERVER_URL", "http://192.168.10.119:8080/v1/chat/completions"
    )


def get_max_token() -> int:
    reload_env()
    try:
        return int(getenv("MAX_TOKEN", "4096"))
    except ValueError:
        return 4096


def get_server_ctx_size() -> int:
    reload_env()
    try:
        return int(getenv("SERVER_CTX_SIZE", "16384"))
    except ValueError:
        return 16384


def get_max_steps() -> int:
    reload_env()
    try:
        return int(getenv("MAX_STEPS", "40"))
    except ValueError:
        return 40


def get_command_timeout() -> int:
    reload_env()
    try:
        return int(getenv("COMMAND_TIMEOUT", "60"))
    except ValueError:
        return 60


def get_workspace_dir() -> Path:
    reload_env()
    custom_ws = getenv("WORKSPACE_DIR")
    if custom_ws:
        p = Path(custom_ws).expanduser().resolve()
    else:
        p = (Path.home() / "discord-bot" / "workspace").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_projects_dir() -> Path:
    p = get_workspace_dir() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_docker_image() -> str:
    reload_env()
    return getenv("DOCKER_IMAGE", "python:3.11-slim")
