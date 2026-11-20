import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_parent_dir = Path(__file__).resolve().parent.parent
BASE_DIR = _parent_dir.parent if _parent_dir.name == "src" else _parent_dir
ENV_PATH = BASE_DIR / ".env"


def reload_env() -> None:
    load_dotenv(dotenv_path=ENV_PATH, override=True)


def get_env_str(key: str) -> str | None:
    reload_env()
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return None
    return val.strip()


def get_env_int(key: str) -> int | None:
    raw = get_env_str(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_env_float(key: str) -> float | None:
    raw = get_env_str(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def get_env_bool(key: str) -> bool | None:
    raw = get_env_str(key)
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes", "on")


def require_env(key: str) -> str:
    val = get_env_str(key)
    if val is None:
        raise RuntimeError(f"Missing required environment variable: '{key}' in .env")
    return val


def require_env_int(key: str) -> int:
    val = get_env_int(key)
    if val is None:
        raise RuntimeError(f"Missing or invalid required integer environment variable: '{key}' in .env")
    return val


def require_env_float(key: str) -> float:
    val = get_env_float(key)
    if val is None:
        raise RuntimeError(f"Missing or invalid required float environment variable: '{key}' in .env")
    return val


def require_env_bool(key: str) -> bool:
    val = get_env_bool(key)
    if val is None:
        raise RuntimeError(f"Missing required boolean environment variable: '{key}' in .env")
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


# --- Ollama & Models ---
def get_ollama_base_url() -> str:
    base = require_env("OLLAMA_BASE_URL")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base.rstrip("/")


def get_ollama_chat_url() -> str:
    return f"{get_ollama_base_url()}/api/chat"


def get_model_for_role(role: str) -> str:
    role_key = f"MODEL_{role.upper()}"
    model = get_env_str(role_key) or get_env_str("MODEL_MAIN")
    if not model:
        raise RuntimeError(
            f"Model for role '{role}' is not configured. "
            f"Set '{role_key}' or 'MODEL_MAIN' in .env."
        )
    return model


def get_heavy_model_for_role(role: str) -> str:
    role_key = f"MODEL_HEAVY_{role.upper()}"
    model = get_env_str(role_key) or get_env_str("MODEL_HEAVY")
    if not model:
        raise RuntimeError(
            f"Heavy model for role '{role}' is not configured. "
            f"Set '{role_key}' or 'MODEL_HEAVY' in .env."
        )
    return model


def _parse_think_value(raw: str | None) -> bool | str | None:
    if raw is None:
        return None
    low = raw.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return low


def get_ollama_think() -> bool | str | None:
    return _parse_think_value(get_env_str("OLLAMA_THINK"))


def get_heavy_ollama_think() -> bool | str | None:
    return _parse_think_value(get_env_str("HEAVY_THINK"))


def get_think_prompt_boost() -> bool:
    return require_env_bool("THINK_PROMPT_BOOST")


def get_ollama_keep_alive() -> str | None:
    return get_env_str("OLLAMA_KEEP_ALIVE")


def get_heavy_ollama_keep_alive() -> str | None:
    return get_env_str("HEAVY_KEEP_ALIVE")


def get_ollama_options() -> dict[str, Any]:
    opts: dict[str, Any] = {}
    int_mappings = {
        "SERVER_CTX_SIZE": "num_ctx",
        "MAX_TOKEN": "num_predict",
        "LLM_TOP_K": "top_k",
        "LLM_REPEAT_LAST_N": "repeat_last_n",
        "LLM_SEED": "seed",
        "OLLAMA_NUM_GPU": "num_gpu",
        "OLLAMA_NUM_THREAD": "num_thread",
        "OLLAMA_NUM_BATCH": "num_batch",
    }
    for env_key, opt_key in int_mappings.items():
        val = get_env_int(env_key)
        if val is not None:
            opts[opt_key] = val

    float_mappings = {
        "LLM_TEMP": "temperature",
        "LLM_TOP_P": "top_p",
        "LLM_MIN_P": "min_p",
        "LLM_REPEAT_PENALTY": "repeat_penalty",
        "LLM_PRESENCE_PENALTY": "presence_penalty",
        "LLM_FREQ_PENALTY": "frequency_penalty",
    }
    for env_key, opt_key in float_mappings.items():
        val = get_env_float(env_key)
        if val is not None:
            opts[opt_key] = val

    return opts


def get_heavy_ollama_options() -> dict[str, Any]:
    opts: dict[str, Any] = {}
    int_mappings = {
        "HEAVY_CTX_SIZE": "num_ctx",
        "HEAVY_MAX_TOKEN": "num_predict",
        "HEAVY_TOP_K": "top_k",
        "HEAVY_REPEAT_LAST_N": "repeat_last_n",
        "HEAVY_SEED": "seed",
        "HEAVY_NUM_GPU": "num_gpu",
        "HEAVY_NUM_THREAD": "num_thread",
        "HEAVY_NUM_BATCH": "num_batch",
    }
    for env_key, opt_key in int_mappings.items():
        val = get_env_int(env_key)
        if val is not None:
            opts[opt_key] = val

    float_mappings = {
        "HEAVY_TEMP": "temperature",
        "HEAVY_TOP_P": "top_p",
        "HEAVY_MIN_P": "min_p",
        "HEAVY_REPEAT_PENALTY": "repeat_penalty",
        "HEAVY_PRESENCE_PENALTY": "presence_penalty",
        "HEAVY_FREQ_PENALTY": "frequency_penalty",
    }
    for env_key, opt_key in float_mappings.items():
        val = get_env_float(env_key)
        if val is not None:
            opts[opt_key] = val

    return opts


def get_llm_timeout() -> int:
    return require_env_int("LLM_TIMEOUT")


# --- Directories ---
def _resolve_dir(env_key: str) -> Path:
    raw = require_env(env_key)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_dir() -> Path:
    return _resolve_dir("WORKSPACE_DIR")


def get_exports_dir() -> Path:
    return _resolve_dir("EXPORTS_DIR")


def get_log_dir() -> Path:
    return _resolve_dir("LOG_DIR")


# --- Execution Parameters ---
def get_docker_image() -> str:
    return require_env("DOCKER_IMAGE")


def get_docker_mem_limit() -> str | None:
    return get_env_str("DOCKER_MEM_LIMIT")


def get_command_timeout() -> int:
    return require_env_int("COMMAND_TIMEOUT")


def get_max_steps_per_phase() -> int:
    return require_env_int("MAX_STEPS_PER_PHASE")


def get_pipeline_idle_sec() -> int:
    return require_env_int("PIPELINE_IDLE_SEC")


def get_auditor_interval_sec() -> int:
    return require_env_int("AUDITOR_INTERVAL_SEC")


# --- Autonomous Mode Settings ---
def get_autonomous_enable() -> bool:
    return require_env_bool("AUTONOMOUS_ENABLE")


def get_autonomous_idle_sec() -> int:
    return require_env_int("AUTONOMOUS_IDLE_SEC")


def get_autonomous_max_steps() -> int:
    return require_env_int("AUTONOMOUS_MAX_STEPS")


# --- Status Monitor & Hardware Specs ---
def get_ollama_ps_url() -> str:
    return f"{get_ollama_base_url()}/api/ps"


def get_status_channel_id() -> int | None:
    return get_env_int("DISCORD_STATUS_CHANNEL_ID")


def get_status_update_interval_sec() -> int:
    return require_env_int("STATUS_UPDATE_INTERVAL_SEC")


def get_desktop_vram_gb() -> float | None:
    return get_env_float("DESKTOP_VRAM_GB")


def get_desktop_ram_gb() -> float | None:
    return get_env_float("DESKTOP_RAM_GB")


def get_server_ctx_size() -> int | None:
    return get_env_int("SERVER_CTX_SIZE")


def get_heavy_ctx_size() -> int | None:
    return get_env_int("HEAVY_CTX_SIZE")


def get_raw_log_channel_id() -> int | None:
    return get_env_int("DISCORD_RAW_LOG_CHANNEL_ID")


# --- NAS (WebUI / WebDAV / Samba) Settings ---
def get_nas_enable() -> bool:
    return require_env_bool("NAS_ENABLE")


def get_nas_port() -> int:
    return require_env_int("NAS_PORT")


def get_nas_user() -> str | None:
    return get_env_str("NAS_USER")


def get_nas_password() -> str | None:
    return get_env_str("NAS_PASSWORD")


def get_samba_enable() -> bool:
    return require_env_bool("SAMBA_ENABLE")


def get_samba_port() -> int:
    return require_env_int("SAMBA_PORT")


def get_desktop_metrics_url() -> str | None:
    return get_env_str("DESKTOP_METRICS_URL")


def get_sandbox_env_hint() -> str | None:
    return get_env_str("SANDBOX_ENV_HINT")


# --- Concurrency, Streaming Granularity & Context Budget ---
def get_ollama_num_parallel() -> int | None:
    val = get_env_int("OLLAMA_NUM_PARALLEL")
    return max(1, val) if val is not None else None


def get_stream_granularity_tokens() -> int:
    return max(1, require_env_int("STREAM_GRANULARITY_TOKENS"))


def get_io_chunk_bytes() -> int:
    return max(4096, require_env_int("IO_CHUNK_BYTES"))


def get_max_ctx_chars_budget(is_heavy: bool = False) -> int:
    return require_env_int("CTX_CHAR_BUDGET_HEAVY" if is_heavy else "CTX_CHAR_BUDGET_LIGHT")