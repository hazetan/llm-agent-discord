import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from sandbox.tools import read_workspace_doc

from .config import (
    get_desktop_metrics_url,
    get_desktop_ram_gb,
    get_desktop_vram_gb,
    get_model_for_role,
    get_ollama_ps_url,
    get_server_ctx_size,
)
from .logger import now_jst_str, system_logger

ROLES = ("PM", "SE", "PG", "QA", "AUDITOR")
LINE_MAX_CHARS = 78
TOTAL_TASK_SECTION_LINES = 10

# ANSI Escape Codes (Discord ```ansi 対応)
C_RESET = "\x1b[0m"
C_BOLD = "\x1b[1m"
C_CYAN = "\x1b[1;36m"
C_GREEN = "\x1b[1;32m"
C_YELLOW = "\x1b[1;33m"
C_GRAY = "\x1b[0;30m"
C_WHITE = "\x1b[0;37m"


@dataclass
class BranchEvent:
    role: str
    action: str
    result: str


ROLE_TARGET_STATUSES: dict[str, tuple[str, ...]] = {
    "SE": ("TODO",),
    "PG": ("IMPL", "FIX"),
    "QA": ("QA",),
}


@dataclass
class SystemTelemetry:
    active_roles: set[str] = field(default_factory=set)
    role_ctx_tokens: dict[str, int] = field(default_factory=lambda: {r: 0 for r in ROLES})
    role_tps: dict[str, float] = field(default_factory=lambda: {r: 0.0 for r in ROLES})
    role_branches: dict[str, deque[BranchEvent]] = field(
        default_factory=lambda: {r: deque(maxlen=3) for r in ROLES}
    )

    # log-raw 用の最新ステップ生データ
    raw_role: str = "IDLE"
    raw_model: str = "-"
    raw_step: int = 0
    raw_max_steps: int = 0
    raw_latency: float = 0.0
    raw_tps: float = 0.0
    raw_thought: str = "(待機中...)"
    raw_tool_name: str = "-"
    raw_tool_args: str = "-"
    raw_tool_output: str = "(まだ出力はありません)"
    on_raw_update: Any = None

    @property
    def active_role_label(self) -> str:
        if not self.active_roles:
            return "IDLE"
        return ",".join(sorted(self.active_roles))

    def start_role(self, role: str) -> None:
        r = role.upper()
        self.active_roles.add(r)
        if r in self.role_branches:
            self.role_branches[r].clear()

    def end_role(self, role: str) -> None:
        self.active_roles.discard(role.upper())

    def set_active_role(self, role: str) -> None:
        if role.upper() == "IDLE":
            self.active_roles.clear()
        else:
            self.start_role(role)

    def update_role_tokens(self, role: str, used_tokens: int, tps: float | None = None) -> None:
        r = role.upper()
        if used_tokens > 0:
            self.role_ctx_tokens[r] = used_tokens
        if tps is not None:
            self.role_tps[r] = tps

    def add_branch_event(self, role: str, tool_name: str, args: dict[str, Any], output: str) -> None:
        r = role.upper()
        if tool_name == "run_command":
            target = args.get("command", "")
        elif "path" in args:
            target = args.get("path", "")
        elif "directory" in args:
            target = args.get("directory", "")
        else:
            target = str(args)[:40]

        first_line = output.strip().splitlines()[0] if output.strip() else "(ok)"
        self.role_branches.setdefault(r, deque(maxlen=3)).append(
            BranchEvent(
                role=r,
                action=f"{tool_name}: {target}"[:52],
                result=first_line[:52],
            )
        )

    def record_raw_step(
        self,
        role: str,
        model: str,
        step: int,
        max_steps: int,
        latency: float,
        thought: str,
        tool_name: str = "-",
        tool_args: dict[str, Any] | None = None,
        tool_output: str = "",
        tps: float = 0.0,
    ) -> None:
        import json

        self.raw_role = role.upper()
        self.raw_model = model
        self.raw_step = step
        self.raw_max_steps = max_steps
        self.raw_latency = latency
        self.raw_tps = tps
        self.raw_thought = thought or "(思考テキストなし・直接ツール呼び出し)"
        self.raw_tool_name = tool_name
        self.raw_tool_args = json.dumps(tool_args or {}, ensure_ascii=False)
        self.raw_tool_output = tool_output or "(出力なし)"


telemetry = SystemTelemetry()


def _get_rpi_free_ram_gb() -> float:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception:
        pass
    return 0.0


async def _fetch_ollama_memory() -> dict[str, tuple[float, float]]:
    """Ollama /api/ps からモデルごとの (VRAM_GB, RAM_GB) を取得する"""
    result: dict[str, tuple[float, float]] = {}
    ps_url = get_ollama_ps_url()
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(ps_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = data.get("models", [])
                    for m in models:
                        name = m.get("name", "")
                        total_gb = m.get("size", 0) / (1024**3)
                        vram_gb = m.get("size_vram", 0) / (1024**3)
                        ram_gb = max(0.0, total_gb - vram_gb)
                        result[name] = (vram_gb, ram_gb)
                    system_logger.log("TELEMETRY", f"Ollama /api/ps loaded models ({len(models)}): {result}")
                else:
                    system_logger.log("TELEMETRY", f"Ollama /api/ps returned HTTP {resp.status}", level="WARN")
    except Exception as exc:
        system_logger.log("TELEMETRY", f"Ollama /api/ps fetch failed ({ps_url}): {exc}", level="WARN")
    return result


async def _fetch_desktop_hw_metrics() -> dict[str, float] | None:
    """Desktop側の desktop_metrics_server.py からリアルな VRAM / RAM 実測値を取得する"""
    url = get_desktop_metrics_url()
    if not url:
        system_logger.log("TELEMETRY", "DESKTOP_METRICS_URL is not set in .env (using /api/ps & static config).")
        return None
    timeout = aiohttp.ClientTimeout(total=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    system_logger.log("TELEMETRY", f"Desktop HW metrics fetched: {data}")
                    return data
                system_logger.log("TELEMETRY", f"Desktop HW metrics HTTP {resp.status} from {url}", level="WARN")
    except Exception as exc:
        system_logger.log("TELEMETRY", f"Desktop HW metrics fetch failed ({url}): {exc}", level="WARN")
    return None

def _format_tok_compact(tokens: int) -> str:
    if tokens >= 10000:
        return f"{tokens / 1000:5.1f}k"
    return f"{tokens:6,d}"


def _make_bar(ratio: float, width: int = 10) -> str:
    clamped = max(0.0, min(1.0, ratio))
    filled = int(round(clamped * width))
    return "█" * filled + "░" * (width - filled)


def _visual_width(text: str) -> int:
    """全角文字を2、半角文字を1として見た目上の横幅を計算する"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1 for ch in text)



def _truncate_line(text: str, max_width: int = LINE_MAX_CHARS) -> str:
    """改行を除き、見た目上の横幅（全角2/半角1）で末尾を省略する"""
    clean = text.replace("\n", " ").replace("\r", "")
    if _visual_width(clean) <= max_width:
        return clean
    out: list[str] = []
    w = 0
    limit = max(1, max_width - 2)
    for ch in clean:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
        if w + cw > limit:
            break
        out.append(ch)
        w += cw
    return "".join(out) + ".."


def _wrap_ansi_box(lines: list[str], max_len: int = 1990) -> str:
    """Discord 2000文字上限を超えないよう安全に ```ansi ブロックへ収める"""
    body = "\n".join(lines)
    prefix = "```ansi\n"
    suffix = "\n```"
    budget = max_len - len(prefix) - len(suffix)
    if len(body) > budget:
        body = body[: max(0, budget - 4)] + C_RESET
    return f"{prefix}{body}{suffix}"


def _parse_tasks_from_md() -> list[tuple[str, str]]:
    """~/TASK.md からコメント行を除外し、表記揺れを含めて (ステータスタグ, タスク本文) を抽出する"""
    raw = read_workspace_doc("~/TASK.md")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    tasks: list[tuple[str, str]] = []
    pattern = re.compile(
        r"^\s*(?:[-*+]|#+|\d+[.)])?\s*(?:\[[ xX]\]\s*)?(?:\*\*|__)?\s*\[(TODO|IMPL|FIX|QA|DONE)\]\s*(?:\*\*|__)?\s*:?\s*(.+)"
    )
    for line in raw.splitlines():
        m = pattern.match(line)
        if m:
            tasks.append((m.group(1), m.group(2).strip()))
    return tasks


async def render_status_box() -> str:
    """稼働中のロール・タスクのみをハイライトし、実測メモリ・速度(tok/s)・CTXを表示するダッシュボード"""
    ps_map = await _fetch_ollama_memory()
    hw_data = await _fetch_desktop_hw_metrics()

    max_vram = (hw_data.get("vram_total_gb") if hw_data else None) or get_desktop_vram_gb() or 0.0
    max_ram = (hw_data.get("ram_total_gb") if hw_data else None) or get_desktop_ram_gb() or 0.0
    max_ctx = get_server_ctx_size() or 0
    rpi_free = _get_rpi_free_ram_gb()

    if hw_data:
        used_vram = hw_data.get("vram_used_gb", 0.0)
        used_ram = hw_data.get("ram_used_gb", 0.0)
        ollama_real_mem = used_vram + hw_data.get("ollama_ram_gb", 0.0)
    else:
        used_vram = sum(v for v, _ in ps_map.values())
        used_ram = sum(r for _, r in ps_map.values())
        ollama_real_mem = used_vram + used_ram

    free_vram = max(0.0, max_vram - used_vram)
    free_ram = max(0.0, max_ram - used_ram)
    vram_ratio = (used_vram / max_vram) if max_vram > 0 else 0.0
    ram_ratio = (used_ram / max_ram) if max_ram > 0 else 0.0
    vram_pct = vram_ratio * 100
    ram_pct = ram_ratio * 100
    max_ctx_label = f"{max_ctx:,} tok" if max_ctx > 0 else "N/A"

    divider = f"{C_GRAY}{'─' * 80}{C_RESET}"

    lines: list[str] = [
        f"{C_GREEN}● SIer MULTI-AGENT MONITOR{C_RESET}  |  Active: {C_CYAN}{telemetry.active_role_label:<14}{C_RESET}  |  {now_jst_str()}",
        divider,
        f"{C_BOLD}[HARDWARE & MEMORY]{C_RESET} Desktop ({max_vram:.0f}GB VRAM + {max_ram:.0f}GB RAM)  |  RPi4 Free: {rpi_free:.1f}GB",
        f"  VRAM : [{_make_bar(vram_ratio, 14)}] {used_vram:5.1f} / {max_vram:4.1f} GB ({vram_pct:4.1f}%)  |  Free: {free_vram:5.1f} GB",
        f"  RAM  : [{_make_bar(ram_ratio, 14)}] {used_ram:5.1f} / {max_ram:4.1f} GB ({ram_pct:4.1f}%)  |  Free: {free_ram:5.1f} GB",
        "",
        f"{C_BOLD}[ROLES: MEMORY, SPEED & CONTEXT (MAX CTX: {max_ctx_label})]{C_RESET}",
    ]

    total_sys_mem = max_vram + max_ram
    total_ps_mem = sum(v + r for v, r in ps_map.values())

    for role in ROLES:
        try:
            model = get_model_for_role(role)
        except Exception:
            model = "none"

        v_gb, r_gb = ps_map.get(model, (0.0, 0.0))
        if v_gb == 0.0 and r_gb == 0.0:
            for loaded_name, mem_pair in ps_map.items():
                if model.lower() in loaded_name.lower() or loaded_name.lower() in model.lower():
                    v_gb, r_gb = mem_pair
                    break

        ps_role_mem = v_gb + r_gb
        if hw_data and ps_role_mem > 0 and total_ps_mem > 0:
            role_mem = ollama_real_mem * (ps_role_mem / total_ps_mem)
        elif hw_data and len(ps_map) == 1 and ps_role_mem > 0:
            role_mem = ollama_real_mem
        else:
            role_mem = ps_role_mem

        mem_pct = (role_mem / total_sys_mem) * 100 if total_sys_mem > 0 else 0.0
        ctx_tok = telemetry.role_ctx_tokens.get(role, 0)
        ctx_ratio = (ctx_tok / max_ctx) if max_ctx > 0 else 0.0
        tps_val = telemetry.role_tps.get(role, 0.0)

        is_active = role in telemetry.active_roles
        mark = "▶" if is_active else "•"
        color = C_CYAN if is_active else C_WHITE
        short_model = model[:11].ljust(11)

        row_str = (
            f"  {mark} {role:<7} ({short_model}) "
            f"Mem:{role_mem:4.1f}GB({mem_pct:2.0f}%) | "
            f"{tps_val:5.1f}t/s | "
            f"CTX:[{_make_bar(ctx_ratio, 6)}] {_format_tok_compact(ctx_tok)}({ctx_ratio*100:4.1f}%)"
        )
        lines.append(f"{color}{row_str}{C_RESET}")

    lines.append("")
    lines.append(f"{C_BOLD}[TASK PIPELINE & LIVE EXECUTION BRANCHES]{C_RESET}")

    tasks = _parse_tasks_from_md()
    task_lines: list[str] = []

    active_task_map: dict[int, str] = {}
    for act_role in ROLES:
        if act_role not in telemetry.active_roles:
            continue
        target_statuses = ROLE_TARGET_STATUSES.get(act_role, ())
        for idx, (st, _) in enumerate(tasks):
            if st in target_statuses and idx not in active_task_map:
                active_task_map[idx] = act_role
                break

    active_indices = [i for i in range(len(tasks)) if i in active_task_map]
    pending_indices = [i for i, (st, _) in enumerate(tasks) if st != "DONE" and i not in active_task_map]
    done_indices = [i for i, (st, _) in enumerate(tasks) if st == "DONE"]

    fg_lines: list[str] = []
    for idx in active_indices + pending_indices:
        if len(fg_lines) >= TOTAL_TASK_SECTION_LINES:
            break
        status, title = tasks[idx]
        if idx in active_task_map:
            working_role = active_task_map[idx]
            fg_lines.append(f"{C_YELLOW}{_truncate_line(f'  ▶ [{status}:{working_role}] {title}')}{C_RESET}")

            branch_list = list(telemetry.role_branches.get(working_role, []))
            for b_i, b in enumerate(branch_list):
                if len(fg_lines) >= TOTAL_TASK_SECTION_LINES:
                    break
                arm = "└─" if b_i == len(branch_list) - 1 else "├─"
                sub_arm = "   └─" if b_i == len(branch_list) - 1 else "│  └─"

                fg_lines.append(f"{C_CYAN}{_truncate_line(f'  │  {arm} [{b.role}] {b.action}')}{C_RESET}")
                if len(fg_lines) < TOTAL_TASK_SECTION_LINES:
                    fg_lines.append(f"{C_WHITE}{_truncate_line(f'  │  {sub_arm} Result: {b.result}')}{C_RESET}")
        else:
            fg_lines.append(f"{C_WHITE}{_truncate_line(f'  - [{status}] {title}')}{C_RESET}")

    remaining_slots = max(0, TOTAL_TASK_SECTION_LINES - len(fg_lines))
    recent_done_indices = done_indices[-remaining_slots:] if remaining_slots > 0 else []
    for idx in recent_done_indices:
        _, title = tasks[idx]
        task_lines.append(f"{C_GREEN}{_truncate_line(f'  ✔ [DONE] {title}')}{C_RESET}")

    task_lines.extend(fg_lines)

    if not task_lines:
        task_lines.append(f"{C_WHITE}  (現在登録されているタスクはありません){C_RESET}")

    while len(task_lines) < TOTAL_TASK_SECTION_LINES:
        task_lines.append(" ")

    lines.extend(task_lines)
    return _wrap_ansi_box(lines)


def _format_fixed_block(text: str, fixed_lines: int, max_width: int = 68) -> list[str]:
    """見た目上の横幅（全角2/半角1）で折り返し、必ず fixed_lines 行のリストに整形する"""
    wrapped: list[str] = []
    for raw_line in text.splitlines() or [""]:
        line = raw_line.strip("\r")
        if not line:
            wrapped.append("  ")
            continue
        buf: list[str] = []
        w = 0
        for ch in line:
            cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
            if w + cw > max_width and buf:
                wrapped.append("  " + "".join(buf))
                buf = [ch]
                w = cw
            else:
                buf.append(ch)
                w += cw
        if buf:
            wrapped.append("  " + "".join(buf))

    if len(wrapped) > fixed_lines:
        wrapped = wrapped[: fixed_lines - 1]
        wrapped.append("  ...(以下省略)...")

    while len(wrapped) < fixed_lines:
        wrapped.append(" ")

    return wrapped


def render_raw_log_box() -> str:
    """log-raw チャンネル向けに横幅・上下行数・2000文字上限を厳守した生ログBoxを生成する"""
    divider = f"{C_WHITE}{'─' * 76}{C_RESET}"

    thought_lines = _format_fixed_block(telemetry.raw_thought, fixed_lines=7, max_width=68)
    args_text = f"Tool : {telemetry.raw_tool_name}\nArgs : {telemetry.raw_tool_args}"
    args_lines = _format_fixed_block(args_text, fixed_lines=4, max_width=68)
    output_lines = _format_fixed_block(telemetry.raw_tool_output, fixed_lines=7, max_width=68)

    lines: list[str] = [
        (
            f"{C_YELLOW}⚡ LIVE RAW STREAM{C_RESET} | "
            f"Role: {C_CYAN}{telemetry.raw_role} ({telemetry.raw_model[:16]}){C_RESET} | "
            f"Step: {telemetry.raw_step}/{telemetry.raw_max_steps} "
            f"({telemetry.raw_latency:.1f}s, {telemetry.raw_tps:.1f}t/s) | "
            f"{now_jst_str()}"
        ),
        divider,
        f"{C_BOLD}[1. RAW THOUGHT (モデルの思考プロセス)]{C_RESET}",
        *[f"{C_WHITE}{l}{C_RESET}" for l in thought_lines],
        "",
        f"{C_BOLD}[2. EXECUTED TOOL & ARGUMENTS (実行ツール・引数)]{C_RESET}",
        *[f"{C_CYAN}{l}{C_RESET}" for l in args_lines],
        "",
        f"{C_BOLD}[3. RAW EXECUTION OUTPUT (コマンド・ツール実行結果の生出力)]{C_RESET}",
        *[f"{C_GREEN}{l}{C_RESET}" for l in output_lines],
    ]

    return _wrap_ansi_box(lines)