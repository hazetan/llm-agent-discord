import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_parent_dir = Path(__file__).resolve().parent.parent
BASE_DIR = _parent_dir.parent if _parent_dir.name == "src" else _parent_dir
ENV_PATH = BASE_DIR / ".env"


def reload_env() -> None:
    load_dotenv(dotenv_path=ENV_PATH, override=True)


def get_env_str(key: str, default: str | None = None) -> str | None:
    reload_env()
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip()


def get_env_int(key: str, default: int | None = None) -> int | None:
    raw = get_env_str(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_env_float(key: str, default: float | None = None) -> float | None:
    raw = get_env_str(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def require_env(key: str) -> str:
    val = get_env_str(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: '{key}' in .env")
    return val


# --- Discord ---
def get_discord_token() -> str:
    return require_env("DISCORD_TOKEN")


def get_monitored_channel_ids() -> set[int]:
    raw = get_env_str("DISCORD_CHANNEL_IDS")
    if not raw:
        return set()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


# --- Ollama & Models (Defaults are None for OSS privacy) ---
def get_ollama_base_url() -> str:
    base = require_env("OLLAMA_BASE_URL")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base.rstrip("/")


def get_chat_completions_url() -> str:
    return f"{get_ollama_base_url()}/v1/chat/completions"


def get_model_for_role(role: str) -> str:
    role_key = f"MODEL_{role.upper()}"
    model = get_env_str(role_key) or get_env_str("MODEL_MAIN")
    if not model:
        raise RuntimeError(
            f"Model for role '{role}' is not configured. "
            f"Set '{role_key}' or 'MODEL_MAIN' in .env."
        )
    return model


def get_sampling_params() -> dict[str, Any]:
    params: dict[str, Any] = {}
    temp = get_env_float("LLM_TEMP")
    top_p = get_env_float("LLM_TOP_P")
    max_tokens = get_env_int("MAX_TOKEN")
    ctx_size = get_env_int("SERVER_CTX_SIZE")

    if temp is not None:
        params["temperature"] = temp
    if top_p is not None:
        params["top_p"] = top_p
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if ctx_size is not None:
        params["options"] = {"num_ctx": ctx_size}
    return params


def get_llm_timeout() -> int:
    return get_env_int("LLM_TIMEOUT", 2400) or 2400


# --- Directories ---
def _resolve_dir(env_key: str, fallback_rel: str) -> Path:
    raw = get_env_str(env_key)
    path = Path(raw).expanduser().resolve() if raw else (BASE_DIR / fallback_rel).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_dir() -> Path:
    return _resolve_dir("WORKSPACE_DIR", "workspace")


def get_exports_dir() -> Path:
    return _resolve_dir("EXPORTS_DIR", "exports")


def get_log_dir() -> Path:
    return _resolve_dir("LOG_DIR", "logs")


# --- Execution Parameters ---
def get_docker_image() -> str:
    return get_env_str("DOCKER_IMAGE", "python:3.12-slim") or "python:3.12-slim"


def get_docker_mem_limit() -> str | None:
    return get_env_str("DOCKER_MEM_LIMIT")


def get_command_timeout() -> int:
    return get_env_int("COMMAND_TIMEOUT", 300) or 300


def get_max_steps_per_phase() -> int:
    return get_env_int("MAX_STEPS_PER_PHASE", 20) or 20


def get_pipeline_idle_sec() -> int:
    return get_env_int("PIPELINE_IDLE_SEC", 10) or 10


def get_auditor_interval_sec() -> int:
    return get_env_int("AUDITOR_INTERVAL_SEC", 180) or 180


# --- Status Monitor & Hardware Specs ---
def get_ollama_ps_url() -> str:
    return f"{get_ollama_base_url()}/api/ps"


def get_status_channel_id() -> int | None:
    return get_env_int("DISCORD_STATUS_CHANNEL_ID")


def get_status_update_interval_sec() -> int:
    return get_env_int("STATUS_UPDATE_INTERVAL_SEC", 120) or 120


def get_desktop_vram_gb() -> float:
    return get_env_float("DESKTOP_VRAM_GB", 8.0) or 8.0


def get_desktop_ram_gb() -> float:
    return get_env_float("DESKTOP_RAM_GB", 8.0) or 8.0


def get_server_ctx_size() -> int:
    return get_env_int("SERVER_CTX_SIZE", 4086) or 4086


def get_raw_log_channel_id() -> int | None:
    return get_env_int("DISCORD_RAW_LOG_CHANNEL_ID")


# --- NAS (WebUI / WebDAV / Samba) Settings ---
def get_env_bool(key: str, default: bool = False) -> bool:
    raw = get_env_str(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def get_nas_enable() -> bool:
    return get_env_bool("NAS_ENABLE", False)


def get_nas_port() -> int:
    return get_env_int("NAS_PORT", 8080) or 8080


def get_nas_user() -> str | None:
    return get_env_str("NAS_USER")


def get_nas_password() -> str | None:
    return get_env_str("NAS_PASSWORD")


def get_samba_enable() -> bool:
    return get_env_bool("SAMBA_ENABLE", False)


def get_samba_port() -> int:
    return get_env_int("SAMBA_PORT", 445) or 445