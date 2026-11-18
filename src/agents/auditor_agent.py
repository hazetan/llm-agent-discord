import asyncio

from src.core.config import get_auditor_interval_sec
from src.core.logger import read_recent_logs

from .runner import run_role_phase


async def continuous_auditor_loop() -> None:
    """定期的に各部署のログを監査し、~/REPORT.md を更新する常駐ループ"""
    while True:
        await asyncio.sleep(get_auditor_interval_sec())
        try:
            recent_logs = read_recent_logs(["PM", "SE", "PG", "QA"])
            if "(No logs recorded yet)" in recent_logs:
                continue

            instruction = (
                "以下は各部門の最新稼働ログです。エラーの傾向、パス解決の迷い、差戻しループなどの課題を分析し、"
                "`~/REPORT.md` を最新の監査レポートで更新してください。\n\n"
                f"{recent_logs}"
            )
            await run_role_phase("AUDITOR", instruction, max_steps=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Auditor Error] {exc}")