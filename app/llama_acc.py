import ast
import json
import re
from typing import Any

import aiohttp

from .secrets import (
    get_llama_server_url,
    get_llm_timeout,
    get_max_token,
    get_model_audio,
    get_model_coding,
    get_model_decision,
    get_model_light_work,
    get_model_omni,
    get_model_system_cmd,
    get_model_vision,
    get_ollama_tags_url,
    get_Sampling_config,
)

# 実行時に Discord コマンド (!set_model) から変更可能なオーバーライド辞書
_RUNTIME_ROLE_OVERRIDES: dict[str, str] = {}
# 特定モデルへの強制ロック用 (None なら自動切り替えモード)
_LOCKED_MODEL: str | None = None

ROLE_DESCRIPTIONS: dict[str, str] = {
    "decision": "判断・意図分類・モデルルーティング用",
    "coding": "コーディング・設計・複雑な推論・デバッグ用",
    "system_cmd": "システムコマンド実行・ファイル探索・ツール呼び出し用",
    "light_work": "ちょっとした作業・要約・雑談・軽量生成用",
    "vision": "画像・視覚理解用",
    "audio": "音声処理用",
    "omni": "マルチモーダル・総合対話用",
}

ERROR_KEYWORDS: tuple[str, ...] = (
    "error", "exception", "traceback", "failed",
    "permission denied", "not found", "timed out", "syntaxerror", "typeerror",
)


def get_all_role_models() -> dict[str, str]:
    """現在の全ロールとモデル名の対応辞書を返す．"""
    defaults = {
        "decision": get_model_decision(),
        "coding": get_model_coding(),
        "system_cmd": get_model_system_cmd(),
        "light_work": get_model_light_work(),
        "vision": get_model_vision(),
        "audio": get_model_audio(),
        "omni": get_model_omni(),
    }
    defaults.update(_RUNTIME_ROLE_OVERRIDES)
    return defaults


def get_model_for_role(role: str) -> str:
    """指定されたロールに対応するモデル名を返す．ロック中ならロックされたモデルを返す．"""
    if _LOCKED_MODEL:
        return _LOCKED_MODEL
    models = get_all_role_models()
    return models.get(role, models["light_work"])


def set_model_for_role(role: str, model_name: str) -> str:
    """実行時にロールの割り当てモデルまたはロック設定を変更する．"""
    global _LOCKED_MODEL
    role_key = role.strip().lower()
    if role_key == "auto":
        _LOCKED_MODEL = None
        return "自律モデル切り替えモード (Auto) を有効化しました。"
    if role_key == "lock":
        _LOCKED_MODEL = model_name.strip()
        return f"全ステップの使用モデルを `{_LOCKED_MODEL}` に固定しました。"
    if role_key not in ROLE_DESCRIPTIONS:
        valid = ", ".join([*ROLE_DESCRIPTIONS.keys(), "lock", "auto"])
        return f"無効なロール名 `{role}` です。指定可能なロール: {valid}"

    _RUNTIME_ROLE_OVERRIDES[role_key] = model_name.strip()
    return f"ロール `{role_key}` ({ROLE_DESCRIPTIONS[role_key]}) のモデルを `{model_name.strip()}` に設定しました。"


def get_locked_model() -> str | None:
    return _LOCKED_MODEL


async def fetch_available_ollama_models() -> list[dict[str, Any]]:
    """Ollama の /api/tags から利用可能なモデル一覧を取得する．"""
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(get_ollama_tags_url()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("models", [])
    except Exception:
        pass
    return []


# --- LFM3 / Qwen 思考タグ除去 & 多形式ツールコールパーサー ---
def strip_thinking_tokens(text: str) -> str:
    """LFM3 等が出力する <think>...</think> タグを除去する．"""
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # 閉じタグが切れている場合のケア
    if "<think>" in cleaned and "</think>" not in cleaned:
        cleaned = cleaned.split("<think>")[0]
    return cleaned.strip()


def _coerce_param_value(raw_val: str) -> Any:
    val = raw_val.strip()
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.lower() == "null":
        return None
    if re.fullmatch(r"-?\d+", val):
        try:
            return int(val)
        except Exception:
            pass
    if re.fullmatch(r"-?\d+\.\d+", val):
        try:
            return float(val)
        except Exception:
            pass
    if (val.startswith("{") and val.endswith("}")) or (val.startswith("[") and val.endswith("]")):
        try:
            return json.loads(val)
        except Exception:
            pass
    return val


def parse_any_tool_calls(content: str) -> list[dict[str, Any]]:
    """
    LFM3 の XML 形式 (<tool_call><function=name><parameter=k>v</parameter></function></tool_call>)、
    LFM2.5 の特殊トークン形式 (<|tool_call_start|>...<|tool_call_end|>)、
    または JSON 形式のツール呼び出しを検出し、OpenAI 互換の tool_calls リストに変換する．
    """
    if not content:
        return []

    calls: list[dict[str, Any]] = []
    call_idx = 1

    # 1. LFM3 XML 形式: <tool_call><function=NAME><parameter=KEY>VAL</parameter></function></tool_call>
    xml_func_pattern = re.compile(
        r"<function=([a-zA-Z0-9_]+)>(.*?)</function>", re.DOTALL
    )
    param_pattern = re.compile(
        r"<parameter=([a-zA-Z0-9_]+)>(.*?)</parameter>", re.DOTALL
    )
    for func_match in xml_func_pattern.finditer(content):
        func_name = func_match.group(1).strip()
        func_body = func_match.group(2)
        args_dict: dict[str, Any] = {}
        for p_match in param_pattern.finditer(func_body):
            p_key = p_match.group(1).strip()
            p_val = p_match.group(2)
            # 先頭と末尾の単一改行のみ除去（コード内のインデント等は維持）
            if p_val.startswith("\n"):
                p_val = p_val[1:]
            if p_val.endswith("\n"):
                p_val = p_val[:-1]
            args_dict[p_key] = _coerce_param_value(p_val) if p_key != "content" else p_val

        calls.append({
            "id": f"lfm_xml_call_{call_idx}",
            "type": "function",
            "function": {
                "name": func_name,
                "arguments": json.dumps(args_dict, ensure_ascii=False),
            },
        })
        call_idx += 1

    if calls:
        return calls

    # 2. <tool_call>{...}</tool_call> または <|tool_call_start|>...<|tool_call_end|>
    tag_blocks = re.findall(
        r"(?:<tool_call>|<\|tool_call_start\|>)\s*(.*?)\s*(?:</tool_call>|<\|tool_call_end\|>)",
        content,
        flags=re.DOTALL,
    )
    for block in tag_blocks:
        block_str = block.strip()
        # JSON 形式の場合
        try:
            parsed = json.loads(block_str)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict) and "name" in item:
                    raw_args = item.get("arguments", item.get("args", {}))
                    if not isinstance(raw_args, str):
                        raw_args = json.dumps(raw_args, ensure_ascii=False)
                    calls.append({
                        "id": f"tag_json_call_{call_idx}",
                        "type": "function",
                        "function": {"name": item["name"], "arguments": raw_args},
                    })
                    call_idx += 1
            if calls:
                return calls
        except Exception:
            pass

        # Pythonic 形式: [run_command(command="ls -la")]
        py_inner = block_str.strip("[]").strip()
        if "(" in py_inner and py_inner.endswith(")"):
            try:
                tree = ast.parse(py_inner, mode="eval")
                if isinstance(tree.body, ast.Call) and isinstance(tree.body.func, ast.Name):
                    fn_name = tree.body.func.id
                    kw_args = {}
                    for kw in tree.body.keywords:
                        if kw.arg:
                            kw_args[kw.arg] = ast.literal_eval(kw.value)
                    calls.append({
                        "id": f"py_call_{call_idx}",
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": json.dumps(kw_args, ensure_ascii=False),
                        },
                    })
                    call_idx += 1
            except Exception:
                pass

    if calls:
        return calls

    # 3. Markdown コードブロックまたは裸の JSON
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    candidate_str = md_match.group(1) if md_match else content.strip()
    if candidate_str.startswith("{") and candidate_str.endswith("}"):
        try:
            data = json.loads(candidate_str)
            if isinstance(data, dict) and "name" in data and ("arguments" in data or "args" in data):
                raw_args = data.get("arguments", data.get("args", {}))
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                calls.append({
                    "id": f"fallback_json_{call_idx}",
                    "type": "function",
                    "function": {"name": data["name"], "arguments": raw_args},
                })
        except Exception:
            pass

    return calls


def _sanitize_messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    異なるモデル間で会話履歴を引き継ぐ際、テンプレートエラーを防ぐため
    content=None の空文字化や過去の不要な <think> タグ除去を行う．
    """
    sanitized: list[dict[str, Any]] = []
    for msg in messages:
        new_msg = dict(msg)
        raw_content = new_msg.get("content")
        if raw_content is None:
            new_msg["content"] = ""
        elif isinstance(raw_content, str) and new_msg.get("role") == "assistant":
            new_msg["content"] = strip_thinking_tokens(raw_content)

        if "tool_calls" in new_msg and new_msg["tool_calls"]:
            fixed_calls = []
            for tc in new_msg["tool_calls"]:
                tc_copy = dict(tc)
                if "type" not in tc_copy:
                    tc_copy["type"] = "function"
                if "function" in tc_copy:
                    fn_copy = dict(tc_copy["function"])
                    args = fn_copy.get("arguments", "{}")
                    if isinstance(args, dict):
                        fn_copy["arguments"] = json.dumps(args, ensure_ascii=False)
                    tc_copy["function"] = fn_copy
                fixed_calls.append(tc_copy)
            new_msg["tool_calls"] = fixed_calls

        sanitized.append(new_msg)
    return sanitized


async def _post_completion(payload: dict[str, Any]) -> dict[str, Any]:
    """Ollama の /v1/chat/completions へリクエストを送信し、正規化した message を返す．"""
    client_timeout = aiohttp.ClientTimeout(total=get_llm_timeout())
    model_name = payload.get("model", get_model_for_role("light_work"))
    payload["model"] = model_name

    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(get_llama_server_url(), json=payload) as resp:
            try:
                data = await resp.json()
            except Exception as exc:
                raw_text = await resp.text()
                raise RuntimeError(
                    f"Ollama returned HTTP {resp.status} (model={model_name}): {raw_text}"
                ) from exc

            if resp.status != 200 or "choices" not in data:
                err_detail = data.get("error", data)
                raise RuntimeError(
                    f"Ollama error (HTTP {resp.status}, model={model_name}): {err_detail}"
                )

            message = data["choices"][0]["message"]
            raw_content = message.get("content") or ""

            # ツール呼び出しが content 側に XML や JSON で出力された場合を自動救済
            if not message.get("tool_calls") and raw_content:
                extracted_calls = parse_any_tool_calls(raw_content)
                if extracted_calls:
                    message["tool_calls"] = extracted_calls
                    # ツールタグ部分を本文から除去
                    cleaned_text = re.sub(
                        r"<tool_call>.*?</tool_call>|<\|tool_call_start\|>.*?<\|tool_call_end\|>",
                        "",
                        raw_content,
                        flags=re.DOTALL,
                    )
                    message["content"] = strip_thinking_tokens(cleaned_text)
                else:
                    message["content"] = strip_thinking_tokens(raw_content)
            else:
                message["content"] = strip_thinking_tokens(raw_content)

            if "usage" in data:
                message["_usage"] = data["usage"]
            message["_model_used"] = model_name

            return message


async def ask_llm(
    prompt: str,
    system_prompt: str = "You are a concise and helpful programming assistant.",
    role: str = "light_work",
    model: str | None = None,
) -> str:
    """単発チャット・作業用応答（デフォルトは軽量高速な light_work モデルを使用）．"""
    target_model = model or get_model_for_role(role)
    payload: dict[str, Any] = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": min(2048, get_max_token()),
        "stream": False,
        **get_Sampling_config(use_xtc=False, model_name=target_model),
    }
    try:
        message = await _post_completion(payload)
        content = (message.get("content") or "").strip()
        if not content and role == "decision":
            # 万が一 decision モデルが空文字を返した場合は light_work モデルでフォールバック
            fb_model = get_model_for_role("light_work")
            if fb_model != target_model:
                payload["model"] = fb_model
                message = await _post_completion(payload)
                content = (message.get("content") or "").strip()
        return content
    except Exception as exc:
        return f"Error communicating with Ollama ({target_model}): {exc}"


async def decide_with_d1(
    state_text: str,
    question: str,
    choices: dict[str, str],
    default_choice: str = "",
) -> str:
    """
    判断用モデル (d1-2B:Q4_K_M) を用いて、選択肢 (choices) の中から最も適切なキーを高速判定する．
    万が一 d1-2B で判定ラベルが得られなかった場合は light_work (LFM3-1.7B:Q4_K_M) に自動フォールバックする．
    """
    choice_keys = list(choices.keys())
    if not default_choice and choice_keys:
        default_choice = choice_keys[0]

    options_desc = "\n".join(f"- {k}: {v}" for k, v in choices.items())
    prompt = (
        f"[State / Context]\n{state_text}\n\n"
        f"[Question]\n{question}\n\n"
        f"[Options]\n{options_desc}\n\n"
        f"Respond with ONLY the exact option key ({', '.join(choice_keys)}). Do not add any explanation."
    )

    for attempt_role in ("decision", "light_work"):
        model_name = get_model_for_role(attempt_role)
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "You are a fast and accurate decision classifier. Output only the chosen label."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 32,
            "stream": False,
            "temperature": 0.0,
        }
        try:
            msg = await _post_completion(payload)
            raw_ans = (msg.get("content") or "").strip().upper()
            for key in choice_keys:
                if re.search(rf"\b{re.escape(key.upper())}\b", raw_ans):
                    return key
            for key in choice_keys:
                if key.upper() in raw_ans:
                    return key
        except Exception:
            continue

    return default_choice


async def select_optimal_model_for_step(
    step: int,
    objective: str,
    raw_history: list[dict[str, Any]],
    retry_attempt: int = 0,
) -> tuple[str, str]:
    """
    エージェントループの各ステップにおいて、現在の文脈から最適な (role, model_name) を自律決定する．
    """
    # 1. 手動ロック時
    if _LOCKED_MODEL:
        return "locked", _LOCKED_MODEL

    # 2. リトライ発生時は確実性を最優先してコーディング用大型モデル (Qwen2.5-Coder-14B) に昇格
    if retry_attempt > 0:
        return "coding", get_model_for_role("coding")

    # 3. 初回ステップ (Step 1) はワークスペースのファイル探索 (list_files / read_file) から始めるため system_cmd
    if step == 1 or not raw_history:
        return "system_cmd", get_model_for_role("system_cmd")

    # 直近の履歴を分析
    recent_msgs = raw_history[-4:]
    recent_summary_lines = []
    last_tool_name = ""
    last_tool_had_error = False

    for m in recent_msgs:
        r = m.get("role", "")
        c = str(m.get("content") or "")[:300]
        if r == "tool":
            t_name = m.get("name", "unknown_tool")
            last_tool_name = t_name
            lower_c = c.lower()
            if any(err_k in lower_c for err_k in ERROR_KEYWORDS):
                last_tool_had_error = True
            recent_summary_lines.append(f"Tool [{t_name}] output: {c}")
        elif r == "assistant":
            t_calls = m.get("tool_calls") or []
            called_names = [tc.get("function", {}).get("name", "") for tc in t_calls]
            recent_summary_lines.append(f"Assistant called: {called_names} | thought: {c[:100]}")
        elif r == "user":
            recent_summary_lines.append(f"User: {c}")

    # 4. 直前のツール実行でエラーが発生していた場合は、原因分析と修正のため coding モデルを選択
    if last_tool_had_error:
        return "coding", get_model_for_role("coding")

    # 5. 画像や音声モダリティのキーワードが含まれる場合の特殊ルーティング
    combined_recent = "\n".join(recent_summary_lines).lower()
    if any(ext in combined_recent for ext in (".png", ".jpg", ".jpeg", ".webp", "image", "画像")):
        if " inspect " in combined_recent or "vision" in combined_recent:
            return "vision", get_model_for_role("vision")
    if any(ext in combined_recent for ext in (".wav", ".mp3", ".ogg", "audio", "音声")):
        return "audio", get_model_for_role("audio")

    # 6. 判断モデル (d1-2B:Q4_K_M) による次ステップのフェーズ判定
    state_text = (
        f"Objective: {objective[:300]}\n"
        f"Current Step: {step}\n"
        f"Last Executed Tool: {last_tool_name or 'None'}\n"
        f"Recent History:\n" + "\n".join(recent_summary_lines)
    )
    choices = {
        "CODING": (
            "Write new code, edit existing files (write_file, replace_in_file, insert_into_file), "
            "design architecture (record_plan), or fix bugs."
        ),
        "SYSTEM_CMD": (
            "List directory files (list_files), read file contents (read_file), "
            "run shell commands or tests (run_command), check git diff, or search the web."
        ),
        "LIGHT_WORK": (
            "All implementation and verification are completely done; only need to update plan result, "
            "ask human a simple question, or call finish_task."
        ),
    }

    # ヒューリスティックな初期デフォルト（read_file や list_files の直後は CODING になりやすい）
    default_phase = "CODING" if last_tool_name in ("read_file", "list_files", "record_plan") else "SYSTEM_CMD"
    chosen = await decide_with_d1(
        state_text=state_text,
        question="Which specialist model should handle the very next agent step?",
        choices=choices,
        default_choice=default_phase,
    )

    role_map = {
        "CODING": "coding",
        "SYSTEM_CMD": "system_cmd",
        "LIGHT_WORK": "light_work",
    }
    selected_role = role_map.get(chosen, "coding")
    return selected_role, get_model_for_role(selected_role)


async def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    use_xtc: bool = False,
    role: str = "coding",
    model: str | None = None,
) -> dict[str, Any]:
    """指定されたロールまたはモデルを用いて Function Calling 推論を実行する．"""
    target_model = model or get_model_for_role(role)
    clean_messages = _sanitize_messages_for_ollama(messages)

    payload: dict[str, Any] = {
        "model": target_model,
        "messages": clean_messages,
        "max_tokens": get_max_token(),
        "stream": False,
        **get_Sampling_config(use_xtc=use_xtc, model_name=target_model),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    return await _post_completion(payload)