from typing import Any

import aiohttp

from .secrets import get_llama_server_url, get_max_token

DEFAULT_TIMEOUT_SECONDS = 2400
DEFAULT_TEMPERATURE = 0.3


async def _post_completion(payload: dict[str, Any]) -> dict[str, Any]:
    """llama-server へ POST リクエストを送信し，レスポンスの message オブジェクトを返す．"""
    client_timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)

    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(get_llama_server_url(), json=payload) as resp:
            try:
                data = await resp.json()
            except Exception as exc:
                raw_text = await resp.text()
                raise RuntimeError(
                    f"llama-server returned HTTP {resp.status}: {raw_text}"
                ) from exc

            if resp.status != 200 or "choices" not in data:
                err_detail = data.get("error", data)
                raise RuntimeError(
                    f"llama-server error (HTTP {resp.status}): {err_detail}"
                )

            return data["choices"][0]["message"]


async def ask_llm(
    prompt: str,
    system_prompt: str = "You are a concise and helpful programming assistant.",
) -> str:
    """単発の質問に対する LLM のテキスト応答を返す．"""
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": DEFAULT_TEMPERATURE,
        "max_tokens": min(1024, get_max_token()),
        "stream": False,
    }
    try:
        message = await _post_completion(payload)
        return message.get("content", "")
    except Exception as exc:
        return f"Error communicating with llama-server: {exc}"


async def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Function Calling (Tools) 対応のチャット補完を実行する．"""
    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": DEFAULT_TEMPERATURE,
        "max_tokens": get_max_token(),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    return await _post_completion(payload)