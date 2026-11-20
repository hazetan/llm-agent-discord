import asyncio
import hashlib

from core.config import get_auditor_interval_sec
from core.logger import read_recent_logs, system_logger

from .runner import run_role_phase


async def continuous_auditor_loop() -> None:
    """定期的に各部署のログ変化を検知し、変化時のみ ~/REPORT.md を更新する常駐ループ"""
    last_digest = ""
    system_logger.log("AUDITOR", "continuous_auditor_loop started.")
    while True:
        await asyncio.sleep(get_auditor_interval_sec())
        try:
            recent_logs = read_recent_logs(["PM", "SE", "PG", "QA"])
            if "(No logs recorded yet)" in recent_logs:
                system_logger.log("AUDITOR", "Skipped audit: No logs recorded yet.")
                continue

            digest = hashlib.sha256(recent_logs.encode("utf-8", errors="replace")).hexdigest()
            if digest == last_digest:
                system_logger.log("AUDITOR", "Skipped audit: No changes in logs since last audit.")
                continue

            system_logger.log("AUDITOR", "Log changes detected. Running AUDITOR phase...")
            instruction = (
                "以下は各部門の最新稼働ログです。エラーの傾向、パス解決の迷い、差戻しループなどの課題を分析し、"
                "`~/REPORT.md` を最新の監査レポートで更新してください。\n\n"
                f"{recent_logs}"
            )
            await run_role_phase("AUDITOR", instruction, max_steps=5)
            last_digest = digest
            system_logger.log("AUDITOR", "AUDITOR phase finished.")
        except asyncio.CancelledError:
            system_logger.log("AUDITOR", "continuous_auditor_loop cancelled.")
            raise
        except Exception as exc:
            print(f"[Auditor Error] {exc}")
            system_logger.error("AUDITOR", f"Auditor loop error: {exc}", exc)