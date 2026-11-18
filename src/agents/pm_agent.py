import asyncio
from pathlib import Path

from .runner import run_role_phase

_pm_lock = asyncio.Lock()


async def handle_client_message(user_message: str) -> tuple[str, list[Path]]:
    """
    過去のDiscordチャット履歴は一切参照せず（履歴汚染の防止）、
    今回の発言と現在のワークスペースのドキュメントのみを基にPMが対応する。
    """
    async with _pm_lock:
        instruction = (
            f"【顧客（Discord）からの新規メッセージ】\n{user_message}\n\n"
            "上記メッセージに対し、PMとして要件定義・`~/TASK.md` の更新（または質問への回答・ZIP出力）を行い、"
            "`complete_phase` で顧客への返答を出力してください。"
        )
        summary, zip_strs = await run_role_phase("PM", instruction, max_steps=10)
        zip_paths = [Path(p) for p in zip_strs if Path(p).exists()]
        return summary or "内容を確認し、タスクファイルを更新しました。", zip_paths