from typing import Any

import aiohttp

from .secrets import (
    get_llama_server_url,
    get_llm_timeout,
    get_max_token,
    get_Sampling_config,
)


async def _post_completion(payload: dict[str, Any]) -> dict[str, Any]:
    """llama-server へ POST リクエストを送信し，message オブジェクトと usage 情報を返す．"""
    client_timeout = aiohttp.ClientTimeout(total=get_llm_timeout())

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

            message = data["choices"][0]["message"]
            if "usage" in data:
                message["_usage"] = data["usage"]

            return message


async def ask_llm(
    prompt: str,
    system_prompt: str = "You are a concise and helpful programming assistant.",
) -> str:
    """単発チャット応答（.env のサンプリング設定・XTC を適用）．"""
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": min(2048, get_max_token()),
        "stream": False,
        **get_Sampling_config(use_xtc=True),
    }
    try:
        message = await _post_completion(payload)
        return message.get("content", "")
    except Exception as exc:
        return f"Error communicating with llama-server: {exc}"


async def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    use_xtc: bool = False,
) -> dict[str, Any]:
    """Function Calling 呼び出し（.env の最新設定に動的追従）．"""
    payload: dict[str, Any] = {
        "messages": messages,
        "max_tokens": get_max_token(),
        **get_Sampling_config(use_xtc=use_xtc),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    return await _post_completion(payload)