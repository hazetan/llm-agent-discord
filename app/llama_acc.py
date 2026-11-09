import aiohttp

from .secret import get_llama_server_url, get_max_token


async def ask_llm(
    prompt: str,
    system_prompt: str = "You are a concise and helpful programming assistant.",
) -> str:
    max_token = get_max_token()
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": min(1024, max_token),
        "stream": False,
    }

    client_timeout = aiohttp.ClientTimeout(total=2400)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        try:
            async with session.post(
                get_llama_server_url(), json=payload
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    raw_text = await resp.text()
                    return f"Error: llama-server returned HTTP {resp.status}: {raw_text}"

                if resp.status == 200 and "choices" in data:
                    return data["choices"][0]["message"]["content"]
                else:
                    return f"Error: llama-server returned status {resp.status} ({data})"
        except Exception as e:
            return f"Error connecting to llama-server: {e}"


async def chat_with_tools(messages: list, tools: list | None = None) -> dict:
    payload = {
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": get_max_token(),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    client_timeout = aiohttp.ClientTimeout(total=2400)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(
            get_llama_server_url(), json=payload
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                raw_text = await resp.text()
                raise RuntimeError(
                    f"llama-server error (HTTP {resp.status}): {raw_text}"
                )

            if resp.status != 200 or "choices" not in data:
                err_detail = data.get("error", data)
                raise RuntimeError(
                    f"llama-server error (HTTP {resp.status}): {err_detail}"
                )
            return data["choices"][0]["message"]
