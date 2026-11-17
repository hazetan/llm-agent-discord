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


# --- 認証・Ollama エンドポイント設定 ---
def load_token() -> str:
    return _get_str("DISCORD_TOKEN", "")


def get_ollama_base_url() -> str:
    """Ollama サーバーのベース URL (例: http://192.168.1.10:11434) を返す．"""
    base = _get_str("OLLAMA_BASE_URL", "") or _get_str("OLLAMA_HOST", "")
    if not base:
        legacy = _get_str("LLAMA_SERVER_URL", "")
        if legacy:
            for suffix in ("/v1/chat/completions", "/api/chat", "/v1", "/api"):
                if legacy.endswith(suffix):
                    legacy = legacy[: -len(suffix)]
                    break
            base = legacy
    if not base:
        base = "http://localhost:11434"
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base.rstrip("/")


def get_llama_server_url() -> str:
    """OpenAI 互換の Chat Completions エンドポイントを返す．"""
    return f"{get_ollama_base_url()}/v1/chat/completions"


def get_ollama_api_chat_url() -> str:
    """Ollama ネイティブの /api/chat エンドポイントを返す．"""
    return f"{get_ollama_base_url()}/api/chat"


def get_ollama_tags_url() -> str:
    """Ollama の利用可能モデル一覧取得エンドポイント (/api/tags) を返す．"""
    return f"{get_ollama_base_url()}/api/tags"


def get_ollama_systemone_url() -> str:
    """Liquid AI d1 モデル用の意思決定エンドポイント (/v1/systemone) を返す．"""
    return f"{get_ollama_base_url()}/v1/systemone"


def get_llama_embedding_url() -> str:
    custom_url = _get_str("LLAMA_EMBEDDING_URL", "")
    if custom_url:
        return custom_url
    return f"{get_ollama_base_url()}/v1/embeddings"


# --- ロール別モデル設定 (Ollama) ---
def get_model_decision() -> str:
    """判断・意図分類・ルーティング用モデル"""
    return _get_str("MODEL_DECISION", "d1-2B:Q4_K_M")


def get_model_coding() -> str:
    """コーディング・設計・複雑な推論・デバッグ用モデル"""
    return _get_str("MODEL_CODING", "Qwen2.5-Coder-14B-Instruct:Q4_K_M")


def get_model_system_cmd() -> str:
    """システムコマンド実行・ファイル探索・ツール呼び出し用モデル"""
    return _get_str("MODEL_SYSTEM_CMD", "LFM3-1.7B:Q8_0")


def get_model_light_work() -> str:
    """ちょっとした作業・要約・雑談・軽量テキスト生成用モデル"""
    return _get_str("MODEL_LIGHT_WORK", "LFM3-1.7B:Q4_K_M")


def get_model_vision() -> str:
    """画像・視覚理解用モデル"""
    return _get_str("MODEL_VISION", "LFM3-VL-2B:Q4_K_M")


def get_model_audio() -> str:
    """音声処理用モデル"""
    return _get_str("MODEL_AUDIO", "LFM3-Audio-2B:Q4_K_M")


def get_model_omni() -> str:
    """マルチモーダル・総合対話用モデル"""
    return _get_str("MODEL_OMNI", "LFM3-Omni-2B:Q4_K_M")


def get_ollama_keep_alive() -> str:
    """Ollama がモデルを VRAM に保持する時間"""
    return _get_str("OLLAMA_KEEP_ALIVE", "10m")


# --- トークン・コンテキスト動的スケーリング設定 ---
def get_server_ctx_size() -> int:
    return _get_int("SERVER_CTX_SIZE", 32768)


def get_max_token() -> int:
    ctx_size = get_server_ctx_size()
    raw_max = _get_int("MAX_TOKEN", 4096)
    return min(raw_max, max(1024, ctx_size // 2))


def get_chars_per_token() -> float:
    return max(1.0, _get_float("CHARS_PER_TOKEN", 2.5))


def get_ctx_safety_margin() -> int:
    default_margin = max(1024, min(4096, get_server_ctx_size() // 20))
    return _get_int("CTX_SAFETY_MARGIN", default_margin)


def get_prompt_token_limit() -> int:
    limit = get_server_ctx_size() - get_max_token() - get_ctx_safety_margin()
    return max(2048, limit)


def get_max_output_chars() -> int:
    default_chars = max(4000, min(20000, get_server_ctx_size() // 5))
    return _get_int("MAX_OUTPUT_CHARS", default_chars)


def get_max_history_messages() -> int:
    default_msgs = max(16, (get_server_ctx_size() // 16384) * 16)
    return _get_int("MAX_HISTORY_MESSAGES", default_msgs)


def get_keep_recent_messages() -> int:
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


def get_Sampling_config(use_xtc: bool = False, model_name: str = "") -> dict[str, Any]:
    """Ollama および対象モデルに最適化したサンプリングパラメータを構築して返す．"""
    lower_model = model_name.lower()
    if "d1" in lower_model:
        temp = 0.0
    elif "lfm3" in lower_model:
        # Liquid AI LFM3 推奨の低温度設定
        temp = _get_float("LLM_TEMP_LFM3", 0.1)
    elif "qwen" in lower_model or "coder" in lower_model:
        temp = _get_float("LLM_TEMP_CODING", 0.2)
    else:
        temp = _get_float("LLM_TEMP", 0.2)

    return {
        "temperature": temp,
        "top_p": _get_float("LLM_TOP_P", 0.9),
    }


# --- ワークスペースパス設定 ---
def get_workspace_dir() -> Path:
    custom_ws = _get_str("WORKSPACE_DIR")
    if custom_ws:
        workspace_path = Path(custom_ws).expanduser().resolve()
    else:
        workspace_path = (Path.home() / "discord_bot" / "workspace").resolve()

    workspace_path.mkdir(parents=True, exist_ok=True)
    return workspace_path


def get_projects_dir() -> Path:
    projects_path = get_workspace_dir() / "projects"
    projects_path.mkdir(parents=True, exist_ok=True)
    return projects_path


def get_log_dir() -> Path:
    custom_log_dir = _get_str("LOG_DIR")
    if custom_log_dir:
        log_dir = Path(custom_log_dir).expanduser().resolve()
    else:
        log_dir = (BASE_DIR / ".log").resolve()

    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_session_log_path(session_id: int) -> Path:
    suffix = _get_str("LOG_FILE_SUFFIX", "")
    return get_log_dir() / f"session{session_id}{suffix}"


def get_log_max_output_chars() -> int:
    return _get_int("LOG_MAX_OUTPUT_CHARS", 50000)


def get_log_max_bytes() -> int:
    return _get_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
