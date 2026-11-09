import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"


def reload_env() -> None:
    """常に最新の .env 設定を反映する．"""
    load_dotenv(dotenv_path=ENV_PATH, override=True)


def _get_str(key: str, default: str = "") -> str:
    reload_env()
    return os.getenv(key, default)


def _get_int(key: str, default: int) -> int:
    reload_env()
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    reload_env()
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(key: str, default: bool = True) -> bool:
    reload_env()
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- 認証・エンドポイント設定 ---
def load_token() -> str:
    return _get_str("DISCORD_TOKEN", "")


def get_llama_server_url() -> str:
    return _get_str(
        "LLAMA_SERVER_URL", "http://192.168.10.119:8080/v1/chat/completions"
    )


def get_llama_embedding_url() -> str:
    custom_url = _get_str("LLAMA_EMBEDDING_URL", "")
    if custom_url:
        return custom_url
    return get_llama_server_url().replace("/chat/completions", "/embeddings")


# --- トークン・コンテキスト動的スケーリング設定 ---
def get_server_ctx_size() -> int:
    return _get_int("SERVER_CTX_SIZE", 65536)


def get_max_token() -> int:
    ctx_size = get_server_ctx_size()
    # 万が一 MAX_TOKEN が SERVER_CTX_SIZE 以上に設定されても，全体の半分以下に自動クランプする
    raw_max = _get_int("MAX_TOKEN", 8192)
    return min(raw_max, max(1024, ctx_size // 2))


def get_chars_per_token() -> float:
    return max(1.0, _get_float("CHARS_PER_TOKEN", 2.5))


def get_ctx_safety_margin() -> int:
    """コンテキスト圧縮を発動させる安全マージン（未指定時は CTX の約 5%，最小 1024）．"""
    default_margin = max(1024, min(4096, get_server_ctx_size() // 20))
    return _get_int("CTX_SAFETY_MARGIN", default_margin)


def get_prompt_token_limit() -> int:
    """プロンプト履歴が使用できる最大トークン数の上限を動的に算出する．"""
    limit = get_server_ctx_size() - get_max_token() - get_ctx_safety_margin()
    return max(2048, limit)


def get_max_output_chars() -> int:
    """1回のツール出力で保持する最大文字数（未指定時は CTX サイズに応じて自動スケール）．"""
    default_chars = max(4000, min(20000, get_server_ctx_size() // 5))
    return _get_int("MAX_OUTPUT_CHARS", default_chars)


def get_max_history_messages() -> int:
    """圧縮前に保持する最大メッセージ数（未指定時は CTX 16k あたり 16 件で自動スケール）．"""
    default_msgs = max(16, (get_server_ctx_size() // 16384) * 16)
    return _get_int("MAX_HISTORY_MESSAGES", default_msgs)


def get_keep_recent_messages() -> int:
    """コンテキスト圧縮時に末尾に残す直近メッセージ件数．"""
    default_keep = max(8, get_max_history_messages() // 3)
    return _get_int("KEEP_RECENT_MESSAGES", default_keep)


# --- 実行制限・インフラ設定 ---
def get_max_steps() -> int:
    return _get_int("MAX_STEPS", 512)


def get_command_timeout() -> int:
    return _get_int("COMMAND_TIMEOUT", 300)


def get_llm_timeout() -> int:
    return _get_int("LLM_TIMEOUT", 2400)


def get_docker_image() -> str:
    return _get_str("DOCKER_IMAGE", "python:3.12-slim")


def get_docker_hosts() -> list[str]:
    raw = _get_str("DOCKER_HOSTS", "unix:///var/run/docker.sock")
    return [h.strip() for h in raw.split(",") if h.strip()]


def get_docker_mem_limit() -> str:
    return _get_str("DOCKER_MEM_LIMIT", "512m")


def get_max_concurrent_tasks() -> int:
    return max(1, _get_int("MAX_CONCURRENT_TASKS", 2))


def get_rag_top_k() -> int:
    return _get_int("RAG_TOP_K", 3)


def get_Sampling_config(use_xtc: bool = True) -> dict[str, Any]:
    """LLM 推論用のサンプリングパラメータ一式を .env から動的構築して返す．"""
    return {
        "temperature": _get_float("LLM_TEMP", 0.3),
        "min_p": _get_float("LLM_MIN_P", 0.05),
        "repeat_penalty": _get_float("LLM_REPEAT_PENALTY", 1.05),
        "repeat_last_n": _get_int("LLM_REPEAT_LAST_N", 512),
        "frequency_penalty": _get_float("LLM_FREQ_PENALTY", 0.05),
        "dry_multiplier": _get_float("LLM_DRY_MULTIPLIER", 0.8),
        "dry_base": _get_float("LLM_DRY_BASE", 1.75),
        "dry_allowed_length": _get_int("LLM_DRY_ALLOWED_LEN", 2),
        "xtc_probability": _get_float("LLM_XTC_PROB", 0.5) if use_xtc else 0.0,
        "xtc_threshold": _get_float("LLM_XTC_THRESHOLD", 0.1),
        "cache_prompt": _get_bool("LLM_CACHE_PROMPT", True),
    }


# --- ワークスペースパス設定 ---
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


def get_log_dir() -> Path:
    """セッションログ保存用ディレクトリ (.log) のパスを返す．"""
    custom_log_dir = _get_str("LOG_DIR")
    if custom_log_dir:
        log_dir = Path(custom_log_dir).expanduser().resolve()
    else:
        log_dir = (BASE_DIR / ".log").resolve()

    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_session_log_path(session_id: int) -> Path:
    """
    指定されたセッションIDのログファイルパス（例: .log/session0）を返す．
    .env で LOG_FILE_SUFFIX=.log 等を指定した場合は拡張子を付与可能（デフォルトは拡張子なし）．
    """
    suffix = _get_str("LOG_FILE_SUFFIX", "")
    return get_log_dir() / f"session{session_id}{suffix}"


def get_log_max_output_chars() -> int:
    """ログファイルに記録する1ツールあたりの最大出力文字数（LLM用の切り詰めとは独立）．"""
    return _get_int("LOG_MAX_OUTPUT_CHARS", 50000)