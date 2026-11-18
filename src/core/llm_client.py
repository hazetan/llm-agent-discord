import ast
import asyncio
import json
import re
import time
from typing import Any

import aiohttp

from .config import (
    get_llm_timeout,
    get_max_concurrent_llm,
    get_model_for_role,
    get_ollama_chat_url,
    get_ollama_keep_alive,
    get_ollama_options,
    get_ollama_think,
    get_think_prompt_boost,
)
from .telemetry import telemetry

HIGH_THINKING_DIRECTIVE = (
    "\n\n【最高レベル思考プロセス (Deep Thinking) 指示】\n"
    "ツールを呼び出す前に、必ず現在の状況・ディレクトリ構造・問題の本質・影響範囲・エッジケースを"
    "最高レベルの深さで段階的に思考（Thinking）し、論理的な整合性を検証してから行動してください。"
)


_llm_semaphore: asyncio.Semaphore | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(get_max_concurrent_llm())
    return _llm_semaphore


def extract_thinking_and_clean(raw_content: str, explicit_thinking: str = "") -> tuple[str, str]:
    """
    レスポンスから (思考テキスト, クリーンな本文) を分離・抽出する。
    """
    think_parts: list[str] = []
    if explicit_thinking and explicit_thinking.strip():
        think_parts.append(explicit_thinking.strip())

    if raw_content:
        tag_matches = re.findall(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)
        for m in tag_matches:
            if m.strip():
                think_parts.append(m.strip())

        cleaned = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL)
        if "<think>" in cleaned and "</think>" not in cleaned:
            prefix, unclosed_think = cleaned.split("<think>", 1)
            if unclosed_think.strip():
                think_parts.append(unclosed_think.strip())
            cleaned = prefix
        cleaned = cleaned.strip()
    else:
        cleaned = ""

    combined_think = "\n".join(think_parts).strip()
    return combined_think, cleaned


def _parse_json_lenient(raw: str) -> Any | None:
    s = raw.strip()
    for candidate in (s, re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            return json.loads(candidate)
        except Exception:
            pass
    try:
        val = ast.literal_eval(s)
        if isinstance(val, (dict, list)):
            return val
    except Exception:
        pass
    return None


def extract_fallback_tool_calls(content: str) -> list[dict[str, Any]]:
    """テキスト内に直接出力されたJSONやタグ形式のツール呼び出しを抽出する"""
    if not content:
        return []

    candidates: list[str] = []
    tag_matches = re.findall(
        r"(?:<tool_call>|<\|tool_call_start\|>)\s*(.*?)\s*(?:</tool_call>|<\|tool_call_end\|>)",
        content,
        flags=re.DOTALL,
    )
    candidates.extend(tag_matches)

    md_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
    candidates.extend(md_matches)

    if not candidates and content.strip().startswith("{") and content.strip().endswith("}"):
        candidates.append(content.strip())

    calls: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates, 1):
        parsed = _parse_json_lenient(cand)
        if not parsed:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if isinstance(item, dict) and "name" in item:
                args = item.get("arguments", item.get("args", {}))
                if isinstance(args, str):
                    args = _parse_json_lenient(args) or {}
                calls.append({
                    "id": f"fallback_call_{idx}",
                    "type": "function",
                    "function": {"name": item["name"], "arguments": args},
                })
    return calls


def _prepare_messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama /api/chat 向けにメッセージ配列と tool_calls の arguments (dict形式) を正規化する"""
    prepared: list[dict[str, Any]] = []
    boost = get_think_prompt_boost()

    for idx, m in enumerate(messages):
        msg_copy: dict[str, Any] = {
            "role": m.get("role", "user"),
            "content": m.get("_clean_content", m.get("content") or ""),
        }
        if idx == 0 and msg_copy["role"] == "system" and boost:
            msg_copy["content"] += HIGH_THINKING_DIRECTIVE

        if "tool_calls" in m and m["tool_calls"]:
            norm_calls = []
            for tc in m["tool_calls"]:
                fn = dict(tc.get("function", {}))
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    fn["arguments"] = _parse_json_lenient(raw_args) or {}
                norm_calls.append({"function": fn})
            msg_copy["tool_calls"] = norm_calls

        prepared.append(msg_copy)
    return prepared


async def chat_step(
    role: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], float, str]:
    """Ollama ネイティブ /api/chat で推論を実行し、Thinking とツール呼び出しを取得する"""
    model_name = get_model_for_role(role)
    prepared_msgs = _prepare_messages_for_ollama(messages)
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": prepared_msgs,
        "stream": False,
    }

    think_setting = get_ollama_think()
    if think_setting is not None:
        payload["think"] = think_setting

    options = get_ollama_options()
    if options:
        payload["options"] = options

    keep_alive = get_ollama_keep_alive()
    if keep_alive:
        payload["keep_alive"] = keep_alive

    if tools:
        payload["tools"] = tools

    # 1. 推論開始直前に入力コンテキスト長（概算）を即座に記録（待機中でも0にならないようにする）
    est_chars = len(json.dumps(prepared_msgs, ensure_ascii=False))
    if tools:
        est_chars += len(json.dumps(tools, ensure_ascii=False))
    est_input_tok = max(1, est_chars // 3)
    telemetry.update_role_tokens(role, est_input_tok)

    start_t = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=get_llm_timeout())

    async with _get_llm_semaphore():
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(get_ollama_chat_url(), json=payload) as resp:
                data = await resp.json()

                # もしモデルが "high"/"max" 等の文字列レベル指定に非対応で 400 を返した場合、think=True で自動再試行
                if resp.status == 400 and isinstance(think_setting, str):
                    payload["think"] = True
                    async with session.post(get_ollama_chat_url(), json=payload) as retry_resp:
                        resp = retry_resp
                        data = await retry_resp.json()

                if resp.status != 200 or "message" not in data:
                    raise RuntimeError(f"Ollama Error ({resp.status}, model={model_name}): {data}")

                # 2. 推論完了後に Ollama からの正確な実測トークン数で上書き更新
                prompt_tok = int(data.get("prompt_eval_count") or 0)
                eval_tok = int(data.get("eval_count") or 0)
                api_total_tok = prompt_tok + eval_tok
                telemetry.update_role_tokens(role, max(api_total_tok, est_input_tok))

                msg = data["message"]
                raw_content = msg.get("content") or ""
                explicit_thinking = msg.get("thinking") or msg.get("reasoning") or ""

                thinking_text, clean_content = extract_thinking_and_clean(raw_content, explicit_thinking)

                if not msg.get("tool_calls") and clean_content:
                    fb_calls = extract_fallback_tool_calls(clean_content)
                    if fb_calls:
                        msg["tool_calls"] = fb_calls

                msg["_clean_content"] = clean_content
                if thinking_text and clean_content:
                    msg["content"] = f"[Thinking]\n{thinking_text}\n\n[Response]\n{clean_content}"
                elif thinking_text:
                    msg["content"] = thinking_text
                else:
                    msg["content"] = clean_content

    latency = time.perf_counter() - start_t
    return msg, latency, model_name