import re
import urllib.request

from ddgs import DDGS

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_FETCH_CHARS = 3000


def perform_web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo で Web 検索を実行し，整形された検索結果リストを返す．"""
    if not query:
        return "Error: Search query is empty."

    is_japanese = any(ord(char) > 0x3000 for char in query)
    region = "jp-jp" if is_japanese else "wt-wt"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region=region, max_results=max_results))
            if not results and is_japanese:
                results = list(ddgs.text(query, region="wt-wt", max_results=max_results))

        if not results:
            return "No clear search results found. Try a shorter query (e.g. 1-2 keywords)."

        formatted = []
        for idx, item in enumerate(results, 1):
            title = item.get("title", "")
            snippet = item.get("body", "")
            link = item.get("href", "")
            formatted.append(f"{idx}. {title}\n   {snippet}\n   URL: {link}")

        return "\n".join(formatted)
    except Exception as exc:
        return f"Search error: {exc}"


def fetch_web_page(target_url: str) -> str:
    """指定された URL から HTML を取得し，タグを除去したプレーンテキストを返す．"""
    if not target_url:
        return "Error: 'url' parameter is required."

    req = urllib.request.Request(target_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")

        content = re.sub(
            r"<(script|style).*?>.*?</\1>",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", " ", content)
        normalized_text = " ".join(text.split())

        if len(normalized_text) > MAX_FETCH_CHARS:
            return normalized_text[:MAX_FETCH_CHARS] + "\n...(truncated)..."
        return normalized_text
    except Exception as exc:
        return f"Fetch error: {exc}"