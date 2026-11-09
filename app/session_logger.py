import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .secrets import (
    get_docker_image,
    get_log_max_output_chars,
    get_max_token,
    get_prompt_token_limit,
    get_Sampling_config,
    get_server_ctx_size,
    get_session_log_path,
)
from .token_monitor import TokenUsageSnapshot

JST = timezone(timedelta(hours=9))


def _now_str() -> str:
    now_utc = datetime.now(UTC)
    now_jst = now_utc.astimezone(JST)
    return f"{now_jst.strftime('%Y-%m-%d %H:%M:%S')} JST ({now_utc.strftime('%H:%M:%S')} UTC)"


def _indent_block(text: str, spaces: int = 6) -> str:
    prefix = " " * spaces
    if not text:
        return f"{prefix}(empty)"
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


# --- 今後の開発・分析用の構造化テレメトリデータ定義 ---
@dataclass
class InferenceTelemetry:
    """1回のLLM推論に関する性能・トークン・挙動データ．"""

    latency_sec: float = 0.0
    retries: int = 0
    dispatch_mode: str = "native_tool_calls"  # native_tool_calls / fallback_json / text_only
    thought_content: str = ""
    prompt_tokens_est: int = 0
    prompt_tokens_actual: int = 0
    completion_tokens_actual: int = 0
    total_tokens_actual: int = 0
    category_ratios: dict[str, float] = field(default_factory=dict)

    @property
    def tokens_per_sec(self) -> float:
        if self.latency_sec <= 0 or self.completion_tokens_actual <= 0:
            return 0.0
        return self.completion_tokens_actual / self.latency_sec


@dataclass
class ToolExecutionRecord:
    """1回のツール呼び出しとコマンド・出力結果の詳細記録．"""

    tool_name: str
    args: dict[str, Any]
    duration_sec: float
    status: str  # SUCCESS / ERROR / BLOCKED_REPEAT
    raw_output: str
    sent_output_len: int

    @property
    def primary_target_summary(self) -> str:
        """ツールの種類に応じて最も重要な引数（コマンドやファイル名）を抽出する．"""
        if self.tool_name == "run_command":
            return f"Command      : {self.args.get('command', '')}"
        if "filename" in self.args:
            extra = ""
            if "content" in self.args:
                extra = f" (write {len(str(self.args['content']))} chars)"
            elif "target_marker" in self.args:
                extra = f" (marker: {self.args.get('target_marker')!r}, pos: {self.args.get('position', 'before')})"
            return f"Target File  : {self.args.get('filename')}{extra}"
        if self.tool_name == "web_search":
            return f"Search Query : {self.args.get('query', '')}"
        if self.tool_name == "fetch_url":
            return f"Target URL   : {self.args.get('url', '')}"
        if self.tool_name == "ask_human":
            return f"Question     : {self.args.get('question', '')}"
        return f"Summary      : {json.dumps(self.args, ensure_ascii=False)[:120]}"


@dataclass
class StepLogRecord:
    """1ステップ全体のイベント・推論・ツール実行を束ねるデータクラス．"""

    step: int
    max_steps: int
    interventions: list[str] = field(default_factory=list)
    context_compressed: bool = False
    handover_snapshot: str = ""
    rag_injected_titles: list[str] = field(default_factory=list)
    inference: InferenceTelemetry = field(default_factory=InferenceTelemetry)
    tool_executions: list[ToolExecutionRecord] = field(default_factory=list)


# --- セッションファイルロガー本体 ---
class SessionFileLogger:
    """
    セッションの開始から終了までの全推論・ツール実行・統計データを
    可読性の高いテキスト形式で .log/session{session_id} に逐次出力する．
    """

    def __init__(
        self,
        session_id: int,
        title: str,
        objective: str,
        workspace_dir: Path,
    ) -> None:
        self.session_id = session_id
        self.title = title
        self.objective = objective
        self.workspace_dir = workspace_dir
        self.log_path: Path = get_session_log_path(session_id)

        # セッション全体の累積統計（今後の開発評価用）
        self.start_time = time.perf_counter()
        self.total_steps_executed = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_inference_sec = 0.0
        self.total_tool_sec = 0.0
        self.compression_count = 0
        self.error_count = 0
        self.repeat_block_count = 0
        self.fallback_parse_count = 0
        self.tool_usage_counter: Counter[str] = Counter()

        self._write_session_header()

    def _append_text(self, text: str) -> None:
        """ログファイルへ即座に追記・フラッシュする．"""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
                f.flush()
        except Exception as exc:
            print(f"[SessionFileLogger] Failed to write log to {self.log_path}: {exc}")

    def _write_session_header(self) -> None:
        ctx_size = get_server_ctx_size()
        max_tok = get_max_token()
        prompt_limit = get_prompt_token_limit()
        sampling_cfg = json.dumps(get_Sampling_config(use_xtc=False), ensure_ascii=False)

        header = (
            "#" * 80 + "\n"
            f"# 🚀 AGENT SESSION LOG : {self.log_path.name} (Session ID: {self.session_id})\n"
            "#" * 80 + "\n"
            f"- Title          : {self.title}\n"
            f"- Objective      : {self.objective}\n"
            f"- Started At     : {_now_str()}\n"
            f"- Log File Path  : {self.log_path}\n"
            f"- Workspace Dir  : {self.workspace_dir}\n"
            f"- Docker Image   : {get_docker_image()}\n"
            f"- Context Config : SERVER_CTX_SIZE={ctx_size:,} | MAX_TOKEN={max_tok:,} | PROMPT_LIMIT={prompt_limit:,}\n"
            f"- Sampling Params: {sampling_cfg}\n"
            "#" * 80 + "\n"
        )
        self._append_text(header)

    def build_inference_telemetry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        actual_usage: dict[str, Any] | None,
        latency_sec: float,
        retries: int,
        dispatch_mode: str,
        thought_content: str,
    ) -> InferenceTelemetry:
        """メッセージ配列とAPIレスポンスから推論テレメトリを構築する．"""
        snap = TokenUsageSnapshot.from_context(messages, tools)
        total_est = max(1, snap.prompt_total)

        ratios = {
            "System": round((snap.system_prompt / total_est) * 100, 1),
            "Tools": round((snap.available_tools / total_est) * 100, 1),
            "Objective": round((snap.task_objective / total_est) * 100, 1),
            "RAG/Plan": round((snap.rag_and_handover / total_est) * 100, 1),
            "History": round((snap.conversation_history / total_est) * 100, 1),
        }

        p_actual = (actual_usage or {}).get("prompt_tokens", 0)
        c_actual = (actual_usage or {}).get("completion_tokens", 0)
        t_actual = (actual_usage or {}).get("total_tokens", 0)

        return InferenceTelemetry(
            latency_sec=latency_sec,
            retries=retries,
            dispatch_mode=dispatch_mode,
            thought_content=thought_content,
            prompt_tokens_est=snap.prompt_total,
            prompt_tokens_actual=p_actual,
            completion_tokens_actual=c_actual,
            total_tokens_actual=t_actual,
            category_ratios=ratios,
        )

    def log_step(self, record: StepLogRecord) -> None:
        """1ステップ分の推論・イベント・ツール実行結果一式を整形して書き出す．"""
        self.total_steps_executed = max(self.total_steps_executed, record.step)
        inf = record.inference
        self.total_inference_sec += inf.latency_sec
        self.total_prompt_tokens += inf.prompt_tokens_actual or inf.prompt_tokens_est
        self.total_completion_tokens += inf.completion_tokens_actual

        if record.context_compressed:
            self.compression_count += 1
        if inf.dispatch_mode == "fallback_json":
            self.fallback_parse_count += 1

        ctx_size = get_server_ctx_size()
        max_tok = get_max_token()
        input_tok_base = inf.prompt_tokens_actual or inf.prompt_tokens_est
        ctx_pct = (input_tok_base / ctx_size * 100) if ctx_size else 0.0
        gen_pct = (inf.completion_tokens_actual / max_tok * 100) if max_tok else 0.0

        lines = [
            "=" * 80,
            f"■ STEP {record.step} / {record.max_steps} | {_now_str()}",
            "=" * 80,
        ]

        # 1. コンテキスト・割り込み・RAGイベント
        lines.append("[1. Context & Events]")
        has_event = False
        if record.interventions:
            has_event = True
            for msg in record.interventions:
                lines.append(f"  • User Intervention : {msg}")
        if record.context_compressed:
            has_event = True
            lines.append("  • Context Compressed: TRUE (Old messages pruned & Planner handover injected)")
            lines.append(_indent_block(record.handover_snapshot, spaces=4))
        if record.rag_injected_titles:
            has_event = True
            lines.append(f"  • RAG Knowledge Hit : {', '.join(record.rag_injected_titles)}")
        if not has_event:
            lines.append("  • Event             : None (Standard step execution)")

        # 2. トークン消費データ
        r = inf.category_ratios
        ratio_str = (
            f"System {r.get('System', 0)}% | Tools {r.get('Tools', 0)}% | "
            f"Objective {r.get('Objective', 0)}% | RAG/Plan {r.get('RAG/Plan', 0)}% | "
            f"History {r.get('History', 0)}%"
        )
        lines.extend(
            [
                "",
                "[2. Token Usage Breakdown]",
                f"  • Prompt Tokens     : Est. {inf.prompt_tokens_est:,} tok | "
                f"Actual Input: {inf.prompt_tokens_actual:,} tok ({ctx_pct:.1f}% of {ctx_size:,} CTX)",
                f"  • Output Tokens     : Actual Gen  : {inf.completion_tokens_actual:,} tok "
                f"({gen_pct:.1f}% of MAX_TOKEN={max_tok:,})",
                f"  • Category Ratio    : {ratio_str}",
            ]
        )

        # 3. LLM 推論メトリクスと思考内容
        thought_body = (
            _indent_block(inf.thought_content, spaces=4)
            if inf.thought_content
            else "    (No text thought; invoked tool directly)"
        )
        lines.extend(
            [
                "",
                "[3. LLM Inference & Thought]",
                f"  • Latency & Speed   : {inf.latency_sec:.2f} sec "
                f"({inf.tokens_per_sec:.1f} tokens/sec) | Retries: {inf.retries}",
                f"  • Dispatch Mode     : {inf.dispatch_mode} ({len(record.tool_executions)} tool call(s))",
                "  • Thought Content   :",
                thought_body,
            ]
        )

        # 4. ツール実行トレース
        lines.append("")
        lines.append("[4. Tool Executions]")
        if not record.tool_executions:
            lines.append("  (No tools executed in this step)")
        else:
            log_max_chars = get_log_max_output_chars()
            for idx, tex in enumerate(record.tool_executions, 1):
                self.tool_usage_counter[tex.tool_name] += 1
                self.total_tool_sec += tex.duration_sec
                if tex.status == "ERROR":
                    self.error_count += 1
                elif tex.status == "BLOCKED_REPEAT":
                    self.repeat_block_count += 1

                raw_len = len(tex.raw_output)
                was_truncated_for_llm = raw_len > tex.sent_output_len
                display_out = tex.raw_output
                if raw_len > log_max_chars:
                    display_out = (
                        display_out[:log_max_chars]
                        + f"\n...(log file truncated after {log_max_chars:,} of {raw_len:,} chars)..."
                    )

                # write_file / replace_in_file 等の長大な content 引数はログの可読性のため文字数表記に要約
                compact_args = dict(tex.args)
                for bulk_key in ("content", "old_text", "new_text"):
                    if bulk_key in compact_args and len(str(compact_args[bulk_key])) > 300:
                        val_str = str(compact_args[bulk_key])
                        compact_args[bulk_key] = (
                            f"{val_str[:120]} ... [+{len(val_str) - 120} chars]"
                        )

                lines.extend(
                    [
                        "  " + "-" * 76,
                        f"  ▶ Tool #{idx}: [{tex.tool_name}]",
                        f"    - Status       : {tex.status} ({tex.duration_sec:.2f} sec)",
                        f"    - {tex.primary_target_summary}",
                        f"    - Arguments    : {json.dumps(compact_args, ensure_ascii=False)}",
                        f"    - Output Size  : {raw_len:,} chars "
                        f"(Sent to LLM: {tex.sent_output_len:,} chars | Truncated for LLM: {was_truncated_for_llm})",
                        "    - Output Body  :",
                        _indent_block(display_out, spaces=6),
                    ]
                )
            lines.append("  " + "-" * 76)

        lines.append("")
        self._append_text("\n".join(lines))

    def log_session_summary(self, final_status: str, final_result: str) -> None:
        """セッション終了時の統計サマリーと最終報告をフッターとして書き出す．"""
        total_elapsed = time.perf_counter() - self.start_time
        avg_tps = (
            self.total_completion_tokens / self.total_inference_sec
            if self.total_inference_sec > 0
            else 0.0
        )
        tool_breakdown = (
            ", ".join(f"{name}: {cnt}" for name, cnt in self.tool_usage_counter.most_common())
            or "None"
        )

        footer = (
            "\n" + "#" * 80 + "\n"
            f"# 🏁 SESSION SUMMARY : {self.log_path.name} (Status: {final_status.upper()})\n"
            "#" * 80 + "\n"
            f"- Finished At          : {_now_str()}\n"
            f"- Total Elapsed Time   : {total_elapsed:.2f} sec "
            f"(LLM Inference: {self.total_inference_sec:.2f}s | Tool Exec: {self.total_tool_sec:.2f}s)\n"
            f"- Total Steps Executed : {self.total_steps_executed}\n"
            f"- Cumulative Tokens    : Input {self.total_prompt_tokens:,} tok | "
            f"Output {self.total_completion_tokens:,} tok | Avg Speed: {avg_tps:.1f} tok/s\n"
            f"- Tool Call Breakdown  : {tool_breakdown}\n"
            f"- Diagnostic Counters  : Tool Errors={self.error_count} | "
            f"Repeat Blocks={self.repeat_block_count} | "
            f"JSON Fallbacks={self.fallback_parse_count} | "
            f"Context Compressions={self.compression_count}\n"
            "-" * 80 + "\n"
            "[Final Result / Report]\n"
            f"{final_result}\n"
            "#" * 80 + "\n"
        )
        self._append_text(footer)