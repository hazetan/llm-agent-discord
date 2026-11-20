import asyncio
from pathlib import Path

from core.logger import system_logger

from .runner import run_role_phase

_pm_lock = asyncio.Lock()


async def handle_client_message(user_message: str) -> tuple[str, list[Path]]:
    """
    過去のDiscordチャット履歴は一切参照せず（履歴汚染の防止）、
    今回の発言と現在のワークスペースのドキュメントのみを基にPMが対応する。
    """
    system_logger.log("PM_AGENT", f"Client message received (len={len(user_message)}). Waiting for _pm_lock...")
    async with _pm_lock:
        system_logger.log("PM_AGENT", "Acquired _pm_lock. Running PM phase...")
        instruction = (
            f"【顧客（Discord）からの新規メッセージ】\n{user_message}\n\n"
            "上記メッセージに対し、PMとして要件定義・`~/TASK.md` の更新（または質問への回答・ZIP出力）を行い、"
            "`complete_phase` で顧客への返答を出力してください。"
        )
        try:
            summary, zip_strs = await run_role_phase("PM", instruction, max_steps=10)
        except Exception as exc:
            system_logger.error("PM_AGENT", f"PM phase failed: {exc}", exc)
            return f"推論エラーが発生しました (PM): {exc}", []

        zip_paths = [Path(p) for p in zip_strs if Path(p).exists()]
        if not summary.strip():
            system_logger.log("PM_AGENT", "PM phase reached max steps without complete_phase.", level="WARN")
            return "処理ステップの上限に達したため完了できませんでした。再度お試しください。", zip_paths
        system_logger.log("PM_AGENT", f"PM phase succeeded (reply_len={len(summary)}, zips={len(zip_paths)}).")
        return summary, zip_paths