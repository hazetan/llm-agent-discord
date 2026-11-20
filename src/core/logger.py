import json
import traceback
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import get_log_dir

JST = timezone(timedelta(hours=9))
MAX_LOG_BYTES = 5 * 1024 * 1024


def now_jst_str() -> str:
    return datetime.now(UTC).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")


class RoleLogger:
    """役職（PM, SE, PG, QA, AUDITOR）ごとの洗練されたログ出力クラス"""

    def __init__(self, role: str) -> None:
        self.role = role.upper()
        self.log_path: Path = get_log_dir() / f"{role.lower()}.log"

    def _write(self, block: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size >= MAX_LOG_BYTES:
            backup = self.log_path.with_suffix(".old.log")
            self.log_path.replace(backup)

        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(block + "\n")
            f.flush()

    def log_phase_start(self, model: str, objective: str) -> None:
        header = (
            f"\n{'=' * 76}\n"
            f"[{now_jst_str()}] ▶ ROLE: {self.role} | MODEL: {model}\n"
            f"OBJECTIVE: {objective}\n"
            f"{'=' * 76}"
        )
        self._write(header)

    def log_step(
        self,
        step: int,
        thought: str,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_output: str | None = None,
        latency_sec: float = 0.0,
        tps: float = 0.0,
    ) -> None:
        compact_args = {}
        if tool_args:
            for k, v in tool_args.items():
                s = str(v)
                compact_args[k] = s[:200] + f"...(+{len(s)-200} chars)" if len(s) > 200 else s

        out_preview = ""
        if tool_output:
            out_preview = (
                tool_output[:800] + f"\n...(+{len(tool_output)-800} chars)"
                if len(tool_output) > 800
                else tool_output
            )

        tps_str = f" | {tps:.1f} tok/s" if tps > 0 else ""
        lines = [
            f"--- [Step {step} | {now_jst_str()} | {latency_sec:.2f}s{tps_str}] ---",
        ]
        if thought:
            lines.append(f"Thought : {thought}")
        if tool_name:
            lines.append(f"Tool    : {tool_name}({json.dumps(compact_args, ensure_ascii=False)})")
        if out_preview:
            lines.append(f"Output  :\n{out_preview}")

        self._write("\n".join(lines))

    def log_error(self, error_msg: str) -> None:
        self._write(f"[{now_jst_str()}] ❌ ERROR ({self.role}): {error_msg}")


def read_recent_logs(roles: list[str], max_chars_per_role: int = 4000) -> str:
    """Auditor用に各ロールの最新ログ末尾を収集する"""
    sections: list[str] = []
    log_dir = get_log_dir()
    for role in roles:
        path = log_dir / f"{role.lower()}.log"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        tail = text[-max_chars_per_role:] if len(text) > max_chars_per_role else text
        sections.append(f"=== RECENT LOG: {role.upper()} ===\n{tail}")
    return "\n\n".join(sections) if sections else "(No logs recorded yet)"


class SystemLogger:
    """起動ごとにリセットされるシステム全体の挙動ダンプ用ロガー (logs/system_debug.log)"""

    def __init__(self) -> None:
        self.log_path: Path = get_log_dir() / "system_debug.log"

    def reset(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"{'=' * 80}\n"
            f"[{now_jst_str()}] 🚀 SYSTEM DEBUG LOG INITIALIZED (RESET ON STARTUP)\n"
            f"{'=' * 80}\n"
        )
        self.log_path.write_text(header, encoding="utf-8")

    def log(self, component: str, message: str, level: str = "INFO") -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{now_jst_str()}] [{level:<5}] [{component:<14}] {message}\n"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

    def error(self, component: str, message: str, exc: BaseException | None = None) -> None:
        tb = f"\n{traceback.format_exc().rstrip()}" if exc else ""
        self.log(component, f"{message}{tb}", level="ERROR")


system_logger = SystemLogger()