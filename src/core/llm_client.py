import ast
import json
import re
import time
from typing import Any

import aiohttp

from .config import (
    get_chat_completions_url,
    get_llm_timeout,
    get_model_for_role,
    get_sampling_params,
)
from .telemetry import telemetry


def strip_thinking_tokens(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "<think>" in cleaned and "</think>" not in cleaned:
        cleaned = cleaned.split("<think>")[0]
    return cleaned.strip()


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
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                calls.append({
                    "id": f"fallback_call_{idx}",
                    "type": "function",
                    "function": {"name": item["name"], "arguments": args},
                })
    return calls


async def chat_step(
    role: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], float, str]:
    """指定された役職のモデルで1ステップの推論を実行する"""
    model_name = get_model_for_role(role)
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        **get_sampling_params(),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    start_t = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=get_llm_timeout())

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(get_chat_completions_url(), json=payload) as resp:
            data = await resp.json()
            if resp.status != 200 or "choices" not in data:
                raise RuntimeError(f"Ollama Error ({resp.status}, model={model_name}): {data}")

            # コンテキスト消費トークン数を記録
            usage = data.get("usage") or {}
            total_tok = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
            telemetry.update_role_tokens(role, int(total_tok))

            msg = data["choices"][0]["message"]
            raw_content = msg.get("content") or ""
            if not msg.get("tool_calls") and raw_content:
                fb_calls = extract_fallback_tool_calls(raw_content)
                if fb_calls:
                    msg["tool_calls"] = fb_calls
            msg["content"] = strip_thinking_tokens(raw_content)

    latency = time.perf_counter() - start_t
    return msg, latency, model_name